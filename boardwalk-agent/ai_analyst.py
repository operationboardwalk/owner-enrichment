"""
ai_analyst.py — AI-powered analysis engine for boardwalk troubleshooting.

Takes raw context from boardwalk's server + a user-described problem,
sends it to Claude or GPT-4, and gets back a diagnosis + fix commands.
"""

import json
import re
import config as cfg_module


SYSTEM_PROMPT = """You are an expert DevOps and AI agent troubleshooting assistant.
You have been given diagnostic data from an AI agent called "boardwalk" running on a Digital Ocean droplet.
Boardwalk is built on the OpenClaw framework, uses ChatGPT as its AI model, and communicates via Telegram.

Your job is to:
1. Analyze the provided diagnostic data and the user's reported problem
2. Identify the root cause(s)
3. Provide a clear diagnosis
4. Provide EXACT shell commands to fix the issue (safe, reversible commands only)
5. Explain what each fix command does and why

Format your response as JSON with this structure:
{
  "diagnosis": "Clear explanation of what is wrong and why",
  "severity": "critical|warning|info",
  "fixes": [
    {
      "id": "fix_1",
      "description": "What this fix does",
      "command": "exact shell command to run via SSH",
      "safe": true,
      "requires_confirmation": false
    }
  ],
  "follow_up": "How to verify the fix worked"
}

Rules:
- Only suggest commands you are confident will help
- Mark requires_confirmation=true for anything that deletes data, restarts services, or modifies configs
- If the problem is unclear, ask for more info in the diagnosis field and leave fixes empty
- If you cannot determine what commands to run, explain why in diagnosis
"""


def _call_openai(cfg: dict, user_message: str) -> str:
    try:
        import openai
        client = openai.OpenAI(api_key=cfg["openai_api_key"])
        model = cfg.get("ai_model", "gpt-4o")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.2,
            max_tokens=2000,
        )
        return response.choices[0].message.content
    except Exception as e:
        raise RuntimeError(f"OpenAI API error: {e}")


def _call_anthropic(cfg: dict, user_message: str) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        model = cfg.get("ai_model", "claude-opus-4-6")
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except Exception as e:
        raise RuntimeError(f"Anthropic API error: {e}")


def _call_ai(cfg: dict, user_message: str) -> str:
    provider = cfg.get("ai_provider", "openai")
    if provider == "anthropic" and cfg.get("anthropic_api_key"):
        return _call_anthropic(cfg, user_message)
    elif cfg.get("openai_api_key"):
        return _call_openai(cfg, user_message)
    elif cfg.get("anthropic_api_key"):
        return _call_anthropic(cfg, user_message)
    else:
        raise RuntimeError("No AI API key configured. Please add an OpenAI or Anthropic key in Setup.")


def _parse_response(raw: str) -> dict:
    """Extract JSON from AI response, handling markdown code blocks."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    # Try to find JSON object
    match = re.search(r'\{.*\}', cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Fallback: return raw text as diagnosis
    return {
        "diagnosis": raw,
        "severity": "info",
        "fixes": [],
        "follow_up": "Review the diagnosis above and apply fixes manually if needed.",
    }


def analyze(user_problem: str, raw_context: str, diagnostic_results: list = None, cfg: dict = None) -> dict:
    """
    Analyze boardwalk's state and the user's reported problem using AI.

    Args:
        user_problem: What the user reports is wrong (free text)
        raw_context: Raw SSH output from diagnostics.collect_raw_context()
        diagnostic_results: List of check results from diagnostics.run_all()
        cfg: Config dict (loaded from config.py if not provided)

    Returns:
        dict with keys: diagnosis, severity, fixes, follow_up
    """
    if cfg is None:
        cfg = cfg_module.load()

    # Build the diagnostic summary
    diag_summary = ""
    if diagnostic_results:
        diag_summary = "\n\n=== AUTOMATED DIAGNOSTIC RESULTS ===\n"
        for r in diagnostic_results:
            emoji = {"ok": "✅", "warning": "⚠️", "critical": "🔴", "unknown": "❓"}.get(r["status"], "?")
            diag_summary += f"{emoji} {r['name']}: {r['message']}\n"
            if r.get("details"):
                diag_summary += f"   Details: {r['details'][:200]}\n"

    user_message = f"""USER REPORTED PROBLEM:
{user_problem}

{diag_summary}

=== RAW DIAGNOSTIC DATA FROM BOARDWALK'S SERVER ===
{raw_context[:8000]}

Please analyze this data and provide your diagnosis and fix commands as JSON.
"""

    raw_response = _call_ai(cfg, user_message)
    result = _parse_response(raw_response)

    # Ensure required fields exist
    result.setdefault("diagnosis", "No diagnosis provided")
    result.setdefault("severity", "info")
    result.setdefault("fixes", [])
    result.setdefault("follow_up", "")

    return result


def verify_fix(fix_description: str, post_fix_context: str, cfg: dict = None) -> dict:
    """
    After applying a fix, ask the AI if it worked based on new log/process output.

    Returns dict with: worked (bool), explanation (str), next_steps (str)
    """
    if cfg is None:
        cfg = cfg_module.load()

    user_message = f"""We just applied this fix to boardwalk:
{fix_description}

Here is the server state AFTER the fix:
{post_fix_context[:4000]}

Did the fix work? Respond as JSON:
{{
  "worked": true or false,
  "explanation": "What you see in the output that indicates success or failure",
  "next_steps": "What to do next"
}}
"""
    try:
        raw = _call_ai(cfg, user_message)
        result = _parse_response(raw)
        result.setdefault("worked", False)
        result.setdefault("explanation", raw)
        result.setdefault("next_steps", "")
        return result
    except Exception as e:
        return {"worked": False, "explanation": f"Could not verify: {e}", "next_steps": "Check manually"}


def quick_answer(question: str, cfg: dict = None) -> str:
    """Ask the AI a quick question without full context (for chat-style interaction)."""
    if cfg is None:
        cfg = cfg_module.load()

    simple_prompt = f"""You are a troubleshooting assistant for an AI agent called boardwalk running on Digital Ocean.
The user asks: {question}
Give a concise, helpful answer. If you need server access to answer, say so."""

    try:
        raw = _call_ai(cfg, simple_prompt)
        return raw
    except Exception as e:
        return f"Error: {e}"
