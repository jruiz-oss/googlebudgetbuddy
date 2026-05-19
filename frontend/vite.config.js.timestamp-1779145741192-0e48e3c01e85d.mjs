import "node:module";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import.meta.url;
var vite_config_default = defineConfig({
	plugins: [react()],
	server: { proxy: { "/api": "http://localhost:5000" } }
});
//#endregion
export { vite_config_default as default };

//# sourceMappingURL=data:application/json;charset=utf-8;base64,eyJ2ZXJzaW9uIjozLCJmaWxlIjoidml0ZS5jb25maWcuanMiLCJuYW1lcyI6W10sInNvdXJjZXMiOlsiL3Nlc3Npb25zL2tpbmQtZ3JlYXQta251dGgvbW50L0dvb2dsZSBCdWRnZXRCdWRkeS9mcm9udGVuZC92aXRlLmNvbmZpZy5qcyJdLCJzb3VyY2VzQ29udGVudCI6WyJpbXBvcnQgeyBkZWZpbmVDb25maWcgfSBmcm9tICd2aXRlJztcbmltcG9ydCByZWFjdCBmcm9tICdAdml0ZWpzL3BsdWdpbi1yZWFjdCc7XG5cbmV4cG9ydCBkZWZhdWx0IGRlZmluZUNvbmZpZyh7XG4gIHBsdWdpbnM6IFtyZWFjdCgpXSxcbiAgc2VydmVyOiB7XG4gICAgcHJveHk6IHtcbiAgICAgICcvYXBpJzogJ2h0dHA6Ly9sb2NhbGhvc3Q6NTAwMCcsXG4gICAgfSxcbiAgfSxcbn0pO1xuIl0sIm1hcHBpbmdzIjoiOzs7O0FBR0EsSUFBQSxzQkFBZSxhQUFhO0NBQzFCLFNBQVMsQ0FBQyxNQUFNLENBQUM7Q0FDakIsUUFBUSxFQUNOLE9BQU8sRUFDTCxRQUFRLHdCQUNWLEVBQ0Y7QUFDRixDQUFDIn0=