# Third-party components in the shipped UI bundle

The pre-built bundle committed at `src/regcompass/ui_dist/` contains these
runtime components (build-only tools like Vite and TypeScript do not ship):

| Component | Version | License |
|---|---|---|
| React + ReactDOM | 19.2.7 | MIT |
| pdfjs-dist (Mozilla PDF.js) | 6.1.200 | Apache-2.0 |
| IBM Plex Sans (via @fontsource/ibm-plex-sans 5.2.8) | - | SIL Open Font License 1.1 |
| IBM Plex Mono (via @fontsource/ibm-plex-mono 5.2.7) | - | SIL Open Font License 1.1 |
| Newsreader (via @fontsource/newsreader 5.3.0) | - | SIL Open Font License 1.1 |
| Noto Sans Lao (via @fontsource/noto-sans-lao 5.3.0) | - | SIL Open Font License 1.1 |

All are Apache-2.0 compatible (project license floor). The fonts are
redistributed under the OFL 1.1. Each one's own license text:

- IBM Plex Sans and IBM Plex Mono: https://github.com/IBM/plex/blob/master/LICENSE.txt
- Newsreader: https://github.com/productiontype/Newsreader/blob/master/OFL.txt
- Noto Sans Lao: https://github.com/notofonts/lao/blob/main/OFL.txt

The same texts are the `LICENSE` files of the `@fontsource/*` packages in
`ui/node_modules` on the dev machine, and the shipped notices file carries
each font's copyright line with the full OFL 1.1 text.

The build also copies PDF.js's WebAssembly image decoders (`jbig2.wasm`,
`openjpeg.wasm`, `qcms_bg.wasm`, their JavaScript fallbacks and their six
upstream `LICENSE_*` texts) out of `pdfjs-dist` into the bundle's `wasm/`
folder, under their own file names because the viewer fetches them by name.
Without them a scanned law renders as a blank page. The shipped notices file,
`notices/THIRD_PARTY_NOTICES.md`, lists each one and its license; that is the
copy that travels inside the wheel.
