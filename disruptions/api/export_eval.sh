#!/usr/bin/env bash
#
# Render the marimo eval notebooks to self-contained static HTML for /eval.
#
# The notebooks (repo-root eval/*.py) run BigQuery ML.PREDICT cells, so we render
# them OFFLINE here — under your ADC, with the repo's full notebook stack — and bake
# the outputs into HTML. main.py then serves that HTML read-only at /eval as plain
# static files, so a public visit costs no BigQuery and spins up no kernel. Run this
# to refresh the results, then commit them and push (CI builds them into the image).
#
# Output: disruptions/api/eval_static/{<notebook>.html, index.html}. That directory is
# committed (unlike demo_static/varsel_static, which CI rebuilds) and shipped in the image.
set -euo pipefail

API_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$API_DIR/../.." && pwd)"
SRC_DIR="$REPO_ROOT/eval"
OUT_DIR="$API_DIR/eval_static"

mkdir -p "$OUT_DIR"

shopt -s nullglob
notebooks=("$SRC_DIR"/*.py)
if [ ${#notebooks[@]} -eq 0 ]; then
  echo "No notebooks found in $SRC_DIR" >&2
  exit 1
fi

# The notebook's own H1: the first `# ...` markdown line in its leading mo.md cell. Skips
# ## sub-headings and (by only scanning after the first mo.md) any earlier code comments.
notebook_title() {
  awk '/mo\.md\(/ {s=1}
       s && /^[[:space:]]*#[[:space:]]+/ {
         sub(/^[[:space:]]*#[[:space:]]+/, ""); sub(/[[:space:]]+$/, ""); print; exit
       }' "$1"
}

# Humanise a filename stem, e.g. pooled-model-bus-eval -> "Pooled model bus eval". Fallback
# for a notebook with no H1.
humanise() {
  local t="${1//[-_]/ }"
  echo "$(tr '[:lower:]' '[:upper:]' <<<"${t:0:1}")${t:1}"
}

# Escape the characters that would otherwise be markup when dropped into HTML text.
html_escape() {
  sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

links=""
for nb in "${notebooks[@]}"; do
  stem="$(basename "$nb" .py)"
  # Prefer the notebook's own top heading as the link text; fall back to the filename.
  title="$(notebook_title "$nb")"
  [ -n "$title" ] || title="$(humanise "$stem")"
  echo ">> exporting $stem (runs its BigQuery cells) ..."
  # --no-include-code strips all source from the HTML: the page shows only the narrative
  # (markdown) + outputs (charts/tables), no code. Set EVAL_SHOW_CODE=1 to include code.
  code_flag="--no-include-code"
  [ "${EVAL_SHOW_CODE:-}" = "1" ] && code_flag="--include-code"
  uv run marimo export html "$nb" -o "$OUT_DIR/$stem.html" -f "$code_flag"
  links+="      <li><a href=\"$stem.html\">$(printf '%s' "$title" | html_escape)</a></li>"$'\n'
done

# A tiny listing page so /eval/ is browsable; links to each rendered notebook.
cat >"$OUT_DIR/index.html" <<EOF
<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Evalueringer</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: system-ui, -apple-system, sans-serif; max-width: 42rem;
           margin: 4rem auto; padding: 0 1.25rem; line-height: 1.5; }
    h1 { font-size: 1.6rem; margin-bottom: .25rem; }
    p.sub { color: #888; margin-top: 0; }
    ul { list-style: none; padding: 0; }
    li { margin: .5rem 0; }
    a { display: block; padding: .85rem 1rem; border: 1px solid #8883; border-radius: .5rem;
        text-decoration: none; color: inherit; font-weight: 600; }
    a:hover { border-color: #888; }
  </style>
</head>
<body>
  <h1>Modell-evalueringer</h1>
  <p class="sub">Forhåndsrendrede marimo-notatbøker (statiske øyeblikksbilder).</p>
  <ul>
$links  </ul>
</body>
</html>
EOF

echo "Wrote $OUT_DIR/index.html and ${#notebooks[@]} notebook page(s)."
