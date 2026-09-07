# Third-Party Notices

NymeriaOS itself is licensed under the PolyForm Noncommercial License 1.0.0
(see `LICENSE`). This file covers the third-party code that is **vendored**,
that is, copied into this repository and redistributed with it. Each component
keeps its own license; nothing here is relicensed.

Ordinary dependencies installed from PyPI or npm are not listed: they are
resolved at install time from their own registries, not copied into this tree.
`Nymeria/pyproject.toml` and `nymeria-desktop/package.json` list them.

---

## 1. Alpine.js

- Vendored at: `nymeria-desktop/src/lib/artifacts/vendor/alpinejs-3.15.12.min.js`
- Version: 3.15.12 (the upstream `dist/cdn.min.js` build)
- Upstream: https://github.com/alpinejs/alpine
- License: MIT (text in "MIT License Text" below)
- Copyright: Caleb Porzio and contributors

Used to make the sandboxed HTML that the `ui_prompt` tool renders interactive
without a network fetch. The minified bundle carries no license header, so the
notice is recorded here.

## 2. Tailwind CSS (browser build)

- Vendored at: `nymeria-desktop/src/lib/artifacts/vendor/tailwindcss-browser-4.3.2.js`
- Version: 4.3.2 (the `@tailwindcss/browser` `dist/index.global.js` build)
- Upstream: https://github.com/tailwindlabs/tailwindcss
- License: MIT (the bundle states `tailwindcss v4.3.2 | MIT License | https://tailwindcss.com`)
- Copyright: Tailwind Labs, Inc.

Compiles utility classes in the browser for the same sandboxed artifact HTML.

## 3. daisyUI

- Vendored at: `nymeria-desktop/src/lib/artifacts/vendor/daisyui-5.6.18.css.txt`
- Version: 5.6.18
- Upstream: https://github.com/saadeghi/daisyui
- License: MIT (the file opens with `/*! daisyUI 5.6.18 - MIT License */`)
- Copyright: Pouya Saadeghi and contributors

Component styles layered on the Tailwind build above.

## 4. LangGraph ReAct agent (derived work)

- Vendored at: `Nymeria/nymeria/vendor/react_agent/`
- Upstream: https://github.com/langchain-ai/langgraph (the prebuilt
  ReAct-agent graph pattern)
- License: MIT (text in "MIT License Text" below)
- Copyright: LangChain, Inc. and contributors

This directory started as a LangGraph ReAct-style agent package and has since
diverged substantially: it is maintained as Nymeria code, with its own
provider factory, checkpointer wrappers, reasoning-token replay, tool-timeout
handling and turn-safety routing. Its `README.md` records the fork policy and
what the fork owns. The notice is kept because the origin is a derived work,
not because the directory tracks an upstream branch.

---

## MIT License Text

Every component above is MIT-licensed, and the MIT terms are identical apart
from the copyright line. The text follows once; read `<copyright holders>` as
the holders named in each section.

```
MIT License

Copyright (c) <copyright holders>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Adding to this file

Vendoring new third-party source means adding a section here in the same pass:
path, version, upstream URL, license, and either the license text or a pointer
to it in-tree. If the new component is not MIT, add its full license text as
its own section rather than reusing the shared text above.
