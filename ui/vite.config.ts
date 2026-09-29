import { createReadStream, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

const resolveFrom = createRequire(import.meta.url)

// PDF.js decodes a scanned page's compressed image in WebAssembly, and it
// fetches those modules at runtime by their own file names from ONE directory.
// A hashed asset name would therefore never be found, so these files are copied
// verbatim into `wasm/` beside index.html rather than run through the asset
// pipeline. Their upstream licence texts travel with them, because that is what
// redistributing the bytes requires. The default is a CDN we must never reach:
// a government scan has to open on a machine with no internet at all.
const WASM_DIR = 'wasm'
const WASM_FILES = [
  // The decoders. jbig2 also serves the CCITT Group 3/4 fax compression that
  // government scanners actually emit; openjpeg serves JPEG 2000 scans.
  'jbig2.wasm',
  'openjpeg.wasm',
  // Colour management, asked for whenever a page carries an ICC profile.
  'qcms_bg.wasm',
  // What the viewer falls back to if a module cannot be instantiated.
  'jbig2_nowasm_fallback.js',
  'openjpeg_nowasm_fallback.js',
  // Upstream licence texts, kept beside the files they cover.
  'LICENSE_JBIG2',
  'LICENSE_OPENJPEG',
  'LICENSE_QCMS',
  'LICENSE_PDFJS_JBIG2',
  'LICENSE_PDFJS_OPENJPEG',
  'LICENSE_PDFJS_QCMS',
]

function pdfWasmSourceDir(): string {
  return join(dirname(resolveFrom.resolve('pdfjs-dist/package.json')), 'wasm')
}

/** Copy the viewer's wasm modules into the bundle under their own names, and
 *  serve them from the same path while developing. */
function vendorPdfWasm(): Plugin {
  const source = pdfWasmSourceDir()
  return {
    name: 'regcompass-vendor-pdf-wasm',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = (req.url ?? '').split('?')[0]
        const prefix = `/${WASM_DIR}/`
        if (!path.startsWith(prefix)) return next()
        const name = path.slice(prefix.length)
        if (!WASM_FILES.includes(name)) return next()
        res.setHeader(
          'Content-Type',
          name.endsWith('.wasm')
            ? 'application/wasm'
            : name.endsWith('.js')
              ? 'text/javascript'
              : 'text/plain',
        )
        createReadStream(join(source, name)).pipe(res)
      })
    },
    generateBundle() {
      for (const name of WASM_FILES) {
        this.emitFile({
          type: 'asset',
          fileName: `${WASM_DIR}/${name}`,
          source: readFileSync(join(source, name)),
        })
      }
    },
  }
}

// The build output is COMMITTED at src/regcompass/ui_dist so judges serve it
// from a pip-only install; Node exists only on the dev machine.
export default defineConfig({
  plugins: [react(), vendorPdfWasm()],
  base: './',
  // THE source of the notices file that MUST travel with the bundle: the OFL
  // 1.1 requires IBM Plex's license text to ship beside the fonts, and
  // emptyOutDir wipes the output directory on every build. Keeping the file
  // here means the build copies it back in rather than a rebuild silently
  // dropping it.
  publicDir: 'notices',
  build: {
    outDir: '../src/regcompass/ui_dist',
    emptyOutDir: true,
  },
  server: {
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
