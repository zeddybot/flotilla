"""Native model configuration; favorites and picker labels are irrelevant."""

MODELS = {
    "Fable 5.2": "fable-5-2",
    "Fable 5.1": "fable-5-1",
    "Opus 5.5": "opus-5-5-1m",
    "Sonnet 5 1M": "sonnet-5-1m",
    "Sonnet 5.5": "sonnet-5-5-1m",
    "Haiku 4.5": "haiku-4-5",
    "Haiku 5.5": "haiku-5-5-1m",
    "GPT-6 Astra": "gpt-6-astra",
    "GPT-6 Sol": "gpt-6-sol",
    "GPT-6.1 Sol": "gpt-6.1-sol",
    "GPT-5.6 Terra": "gpt-5.6-terra",
    "GPT-6 Luna": "gpt-6-luna",
    "Union Alpha Free": "opencode:opencode/union-alpha",
}


def model_id(label):
    if label == "5.6 Terra":
        label = "GPT-" + label
    return MODELS.get(label)


def configuration(options, default):
    model = model_id(options["model"])
    warnings = []
    if model is None:
        model = model_id(default) or "fable-5-2"
        warnings.append(f"requested model {options['model']} unavailable; using {default}")
    levels = ["low", "medium", "high", "xhigh", "max"]
    if model in ("fable-5-1", "fable-5-2", "opus-5-5-1m"):
        levels.append("ultracode")
    elif model in ("gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-terra"):
        levels.append("ultra")
    effort = options["effort"]
    if model == "haiku-4-5" or model.startswith("opencode:"):
        effort = None
    elif effort not in levels:
        warnings.append(f"effort {effort} unavailable; using high")
        effort = "high"
    fast = options["fast"] and (model.startswith("gpt-") or model in ("opus-5-5-1m", "fable-5-2"))
    if options["fast"] and not fast:
        warnings.append("fast mode unavailable; using standard speed")
    agent = (
        "acp"
        if model.startswith("opencode:")
        else "codex"
        if model.startswith("gpt-")
        else "claude"
    )
    return {
        "agent": agent,
        "model": model,
        **({"thinkingLevel": effort} if effort else {}),
        "fastMode": fast,
    }, "; ".join(warnings)
