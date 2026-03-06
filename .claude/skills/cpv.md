---
name: cpv
description: Bump versions where needed, then commit all changes with a brief single line message and push everything
---

1. Identify which packages have changes by running `git diff --stat` in each repo directory
2. Bump patch version in each changed package's version file
3. Stage changed files in each repo (use `git add` with specific files, not `-A`)
4. Commit with a SHORT SINGLE LINE message (no body, no co-author, no bullet points) — use `git commit -m "message"` directly
5. Push each repo separately
6. IMPORTANT: Each package is a separate git repo — always `cd` to the correct directory before git commands:
   - runcore: /Users/stan/Projects/runtools/runcore
   - runjob: /Users/stan/Projects/runtools/runjob
   - runcli: /Users/stan/Projects/runtools/runcli
   - taro: /Users/stan/Projects/runtools/taro
   - runtools (meta): /Users/stan/Projects/runtools/runtools
