import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const backendTarget = process.env.BACKEND_PROXY_TARGET || 'http://127.0.0.1:8000';
const behaviourTarget = process.env.BEHAVIOUR_PROXY_TARGET || 'http://127.0.0.1:8100';

export function forwardVerifiedProxyClientAddress(proxy) {
  const setClientAddress = (proxyRequest, request) => {
    proxyRequest.setHeader('X-Winner-Proxy-Client', request.socket?.remoteAddress || 'unknown');
  };
  proxy.on('proxyReq', setClientAddress);
  proxy.on('proxyReqWs', setClientAddress);
}

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-react':   ['react', 'react-dom', 'react-router-dom'],
          'vendor-charts':  ['recharts'],
        },
      },
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      // Keep the independent system's API routed to its own service. Do not
      // attach Winner Predict's proxy identity header or backend credentials.
      '/api/v1/behaviour': {
        target: behaviourTarget,
        changeOrigin: true,
      },
      '/api': {
        target: backendTarget,
        changeOrigin: true,
        ws: true,
        configure: forwardVerifiedProxyClientAddress,
      },
      '/health': {
        target: backendTarget,
        changeOrigin: true,
        configure: forwardVerifiedProxyClientAddress,
      },
      '/docs': {
        target: backendTarget,
        changeOrigin: true,
        configure: forwardVerifiedProxyClientAddress,
      },
      '/openapi.json': {
        target: backendTarget,
        changeOrigin: true,
        configure: forwardVerifiedProxyClientAddress,
      },
      '/ws': {
        target: backendTarget,
        changeOrigin: true,
        ws: true,
        configure: forwardVerifiedProxyClientAddress,
      },
    },
  },
});
