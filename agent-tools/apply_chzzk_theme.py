import re
from pathlib import Path

css = Path("agent-tools/chzzk-main.css").read_text(encoding="utf-8")
html_path = Path("app/templates/index.html")
html = html_path.read_text(encoding="utf-8")

marker = "html.theme_dark{--Background-Neutral-Weak"
start = css.find(marker)
if start == -1:
    raise SystemExit("Chzzk dark semantic theme block not found")
end = css.find("}", start)
semantic_block = css[start + len("html.theme_dark") : end]

ref_names = sorted(set(re.findall(r"var\(--Ref-Color-([^)]+)\)", semantic_block)))


def get_ref(name: str):
    m = re.search(rf"--Ref-Color-{re.escape(name)}:([^;{{]+)", css)
    mr = re.search(rf"--Ref-Color-{re.escape(name)}-rgb:([^;{{]+)", css)
    return (
        m.group(1).strip() if m else None,
        mr.group(1).strip() if mr else None,
    )


ref_lines = []
for name in ref_names:
    val, rgb = get_ref(name)
    if val:
        ref_lines.append(f"                --Ref-Color-{name}: {val};")
    if rgb:
        ref_lines.append(f"                --Ref-Color-{name}-rgb: {rgb};")

semantic_tokens = []
for part in semantic_block.split(";"):
    part = part.strip()
    if part.startswith("--"):
        semantic_tokens.append("                " + part + ";")

aliases = """
                /* zzk aliases → Chzzk semantic tokens */
                --bg: var(--Background-Neutral-Weak);
                --bg-1: var(--Surface-Neutral-Weak);
                --bg-2: var(--Surface-Neutral-Base);
                --bg-3: var(--Surface-Neutral-Strong);
                --line: var(--Border-Neutral-Base);
                --line-soft: var(--Border-Neutral-Weak);

                --ink-0: var(--Content-Neutral-Primary);
                --ink-1: var(--Content-Neutral-Cool-Base);
                --ink-2: var(--Content-Neutral-Cool-Weak);
                --ink-3: var(--Content-Neutral-Cool-Weaker);
                --ink-4: var(--Content-Neutral-Warm-Weaker);

                --rec: var(--Content-Accent-Red-Strong);
                --rec-glow: rgba(var(--Content-Accent-Red-Strong-rgb), 0.18);
                --live: var(--Content-Brand-Strong);
                --arm: var(--Content-Accent-Prime-Base);
                --arm-soft: var(--Surface-Brand-Alpha-Weak);
                --ok: var(--Content-Brand-Strong);
                --info: var(--Content-Accent-Blue);
"""

new_root = f"""            :root {{
                /* Chzzk design tokens (dark) */
{chr(10).join(ref_lines)}

{chr(10).join(semantic_tokens)}
{aliases}
                /* shape */
                --r-1: 6px;
                --r-2: 10px;
                --r-3: 14px;

                /* type */
                --sans:
                    "Pretendard Variable", Pretendard, -apple-system,
                    BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR",
                    system-ui, sans-serif;
                --mono:
                    "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas,
                    monospace;

                /* spacing rhythm */
                --s-1: 4px;
                --s-2: 8px;
                --s-3: 12px;
                --s-4: 16px;
                --s-5: 24px;
                --s-6: 36px;
                --s-7: 56px;

                /* motion */
                --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
                --ease-in-out: cubic-bezier(0.65, 0, 0.35, 1);
            }}"""

html = re.sub(
    r"            :root \{.*?\n            \}",
    new_root,
    html,
    count=1,
    flags=re.DOTALL,
)

replacements = [
    ("oklch(100% 0 0 / 0.012)", "hsla(0, 0%, 100%, 0.012)"),
    (
        "background: radial-gradient(\n                    circle at 30% 30%,\n                    oklch(40% 0.05 75),\n                    oklch(22% 0.02 75) 70%\n                )",
        "background: radial-gradient(\n                    circle at 30% 30%,\n                    var(--Surface-Neutral-Strong),\n                    var(--Surface-Neutral-Weaker) 70%\n                )",
    ),
    (
        "background: oklch(from var(--bg) l c h / 0.6);",
        "background: rgba(var(--Surface-Neutral-Weakest-rgb), 0.6);",
    ),
    ("color: oklch(98% 0 0);", "color: var(--Content-Neutral-Inverse);"),
    (
        "background: oklch(from var(--rec) calc(l - 0.04) c h);",
        "background: var(--Content-Accent-Red-Weak);",
    ),
    (
        "color: oklch(from var(--rec) calc(l + 0.08) c h);",
        "color: var(--Content-Accent-Red-Weak);",
    ),
    (
        "border-color: oklch(from var(--rec) l c h / 0.4);",
        "border-color: rgba(var(--Content-Accent-Red-Strong-rgb), 0.4);",
    ),
    (
        "background: oklch(from var(--rec) l c h / 0.06);",
        "background: rgba(var(--Content-Accent-Red-Strong-rgb), 0.06);",
    ),
    (
        "border-color: oklch(from var(--rec) l c h / 0.45);",
        "border-color: rgba(var(--Content-Accent-Red-Strong-rgb), 0.45);",
    ),
    (
        "oklch(from var(--rec) l c h / 0.06) 0%,",
        "rgba(var(--Content-Accent-Red-Strong-rgb), 0.06) 0%,",
    ),
    (
        "box-shadow: 0 0 0 3px oklch(from var(--live) l c h / 0.2);",
        "box-shadow: 0 0 0 3px var(--Surface-Brand-Alpha-Weaker);",
    ),
    (
        "border-color: oklch(from var(--live) l c h / 0.4);",
        "border-color: var(--Border-Brand-Alpha-Weak);",
    ),
    (
        "border-color: oklch(from var(--arm) l c h / 0.3);",
        "border-color: var(--Border-Brand-Alpha-Base);",
    ),
    (
        "background: oklch(from var(--rec) l c h / 0.08);",
        "background: rgba(var(--Content-Accent-Red-Strong-rgb), 0.08);",
    ),
    (
        "color: oklch(from var(--rec) calc(l + 0.1) c h);",
        "color: var(--Content-Accent-Red-Weak);",
    ),
    (
        "background: oklch(from var(--arm) l c h / 0.08);",
        "background: var(--Surface-Brand-Alpha-Weaker);",
    ),
    (
        "background: oklch(8% 0.01 75 / 0.7);",
        "background: rgba(var(--Surface-Neutral-Weakest-rgb), 0.7);",
    ),
    (
        "box-shadow: 0 8px 24px oklch(0% 0 0 / 0.4);",
        "box-shadow: 0 8px 24px var(--Shadow-Strong);",
    ),
]

for old, new in replacements:
    html = html.replace(old, new)

# Brand-forward accents
html = html.replace(
    "border: 1.5px solid var(--arm);",
    "border: 1.5px solid var(--Content-Brand-Strong);",
)
html = html.replace(
    "box-shadow: 0 0 12px var(--arm-soft);",
    "box-shadow: 0 0 12px var(--Surface-Brand-Alpha-Base);",
)
html = html.replace(
    "box-shadow: 0 0 8px var(--arm-soft);",
    "box-shadow: 0 0 8px var(--Surface-Brand-Alpha-Base);",
)
html = html.replace(
    "outline: 2px solid var(--arm);", "outline: 2px solid var(--Content-Brand-Strong);"
)

html = html.replace(
    """            ::selection {
                background: var(--arm);
                color: var(--bg);
            }""",
    """            ::selection {
                background: var(--Surface-Brand-Alpha-Strong);
                color: var(--Content-Neutral-Primary);
            }""",
)

html = html.replace(
    """            .btn-primary {
                background: var(--ink-0);
                color: var(--bg);
                border-color: var(--ink-0);
            }
            .btn-primary:hover {
                background: var(--arm);
                border-color: var(--arm);
                color: var(--bg);
            }""",
    """            .btn-primary {
                background: var(--Content-Brand-Strong);
                color: var(--Content-Neutral-Inverse);
                border-color: var(--Content-Brand-Strong);
            }
            .btn-primary:hover {
                background: var(--Content-Brand-Base);
                border-color: var(--Content-Brand-Base);
                color: var(--Content-Neutral-Inverse);
            }""",
)

html = html.replace(
    """            .check input:checked + .box {
                background: var(--arm);
                border-color: var(--arm);
            }
            .check input:checked + .box::after {
                content: "✓";
                color: var(--bg);""",
    """            .check input:checked + .box {
                background: var(--Content-Brand-Strong);
                border-color: var(--Content-Brand-Strong);
            }
            .check input:checked + .box::after {
                content: "✓";
                color: var(--Content-Neutral-Inverse);""",
)

html = html.replace(
    """            .field input:focus,
            .field select:focus {
                border-color: var(--arm);
                box-shadow: 0 0 0 3px var(--arm-soft);
            }""",
    """            .field input:focus,
            .field select:focus {
                border-color: var(--Border-Brand-Base);
                box-shadow: 0 0 0 3px var(--Surface-Brand-Alpha-Weak);
            }""",
)

html = html.replace('content="#1c1b19"', 'content="#0e0f10"')
html = html.replace(
    "fill='%231c1b19'/><rect x='6' y='6' width='20' height='20' rx='3' fill='none' stroke='%23e0a85b' stroke-width='2'/><circle cx='23' cy='9' r='2.2' fill='%23e84a55'",
    "fill='%230e0f10'/><rect x='6' y='6' width='20' height='20' rx='3' fill='none' stroke='%2300ffa3' stroke-width='2'/><circle cx='23' cy='9' r='2.2' fill='%23ff5454'",
)
html = html.replace(
    'href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap"',
    'href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css"',
)
html = html.replace('<html lang="ko">', '<html lang="ko" class="theme_dark">')
html = html.replace(
    "zzk · the recorder's booth\n               single-file dark operations console",
    "zzk · Chzzk design system\n               single-file dark operations console",
)

html_path.write_text(html, encoding="utf-8")
print("done", "refs", len(ref_names), "semantic", len(semantic_tokens))
