import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // amazon-cognito-identity-js references the Node `global`; map it to the
  // browser's globalThis so it runs in the browser.
  define: {
    global: 'globalThis',
  },
})
