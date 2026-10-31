# Upstream dashboard

`dashboard/dist/` contains unmodified files from
[itechmeat/hermes-workflows](https://github.com/itechmeat/hermes-workflows/tree/352e5ffb751e4798e66dcdf1dbe9a80dff768c0f),
by the Hermes Workflows contributors. Upstream declares the MIT license in its
`package.json` and `pyproject.toml`. The copied bundle retains its embedded
third-party notices. Exact original file hashes are in `dashboard/upstream.json`.
The manifest's entry and CSS paths point to first-party companion files that
add human review controls. `dashboard/compat/` is built from a maintained copy
of the same upstream source in the development-only `frontend/` directory,
with DOM dialogs replacing native browser prompts. Original assets remain in
`dashboard/dist/`; both original and maintained assets have recorded hashes.

`desktop/plugin.js` is a first-party Desktop SDK adapter bundling the same
maintained editor and its dependencies (@xyflow/react, @base-ui/react, and
react-dom). React and the Hermes Desktop SDK are provided by the host. Embedded
dependency license comments are retained. The Desktop artifact hash is recorded
in `desktop/assets.json`; its source is `frontend/src/desktop/`.

MIT License

Copyright (c) Hermes Workflows contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
