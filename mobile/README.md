# App móvil (Android) con Capacitor + WebView

Esta app nativa es un contenedor ligero que abre tu instancia de fichaje. El usuario escribe su dominio (o lo escanea en un QR) y la app lo recuerda y mantiene la sesión con cookies del WebView.

## Requisitos
- Node.js 18+
- Java + Android SDK (Android Studio instalado) para compilar APK

## Pasos rápidos de creación
```bash
# 1) Crear proyecto
npm create @capacitor/app@latest mobile-app
cd mobile-app

# 2) Instalar dependencias básicas
npm install

# 3) Instalar plugin de escáner QR
npm install @capacitor-community/barcode-scanner

# 4) Ajustar capacitor.config.ts
# - AppName: Fichaje
# - server: no pongas url fija. Usaremos una pantalla local que pide dominio.
```

## Pantalla inicial (local)
Crea `src/index.html` con:
```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
  <h3>Conectar a tu dominio</h3>
  <input id="domain" placeholder="https://miinstancia.com" />
  <button id="connect">Conectar</button>
  <button id="scan">Escanear QR</button>
  <script type="module">
    import { Browser } from '@capacitor/browser';
    import { Storage } from '@capacitor/storage';
    import { BarcodeScanner } from '@capacitor-community/barcode-scanner';

    const domainInput = document.getElementById('domain');
    document.getElementById('connect').onclick = async () => {
      const url = domainInput.value.trim();
      if (!url) return alert('Introduce un dominio');
      await Storage.set({ key: 'domain', value: url });
      Browser.open({ url });
    };

    document.getElementById('scan').onclick = async () => {
      await BarcodeScanner.checkPermission({ force: true });
      const result = await BarcodeScanner.startScan();
      if (result.hasContent) {
        // Esperamos fichaje://login?domain=...&token=...
        const url = new URL(result.content);
        const domain = url.searchParams.get('domain');
        const token = url.searchParams.get('token');
        if (domain && token) {
          await Storage.set({ key: 'domain', value: domain });
          Browser.open({ url: `${domain}/qr_login?token=${encodeURIComponent(token)}` });
        }
      }
    };

    (async () => {
      const saved = await Storage.get({ key: 'domain' });
      if (saved.value) domainInput.value = saved.value;
    })();
  </script>
</body>
</html>
```

Compila el front local (puedes usar Vite o servir el index tal cual con `npm run build` si eliges plantilla Vanilla).

## Android
```bash
npx cap add android
npx cap copy android
npx cap sync android
npx cap open android   # abre Android Studio
```
En Android Studio, compila/genera el APK (`Build > Build Bundle(s)/APK(s)`).

## QR de login
- El backend ya expone `/qr_login?token=...` usando un token firmado (ver `app_core/routes/auth_routes.py`).
- Genera tokens en consola Flask:
  ```python
  from app_core.routes.auth_routes import generar_token_qr
  generar_token_qr("usuario")
  ```
- Codifica en el QR: `fichaje://login?domain=https://tu-dominio.com&token=<TOKEN>`
  (El botón “Escanear QR” abrirá ese dominio y consumirá `/qr_login` con el token).

## Notas de sesión
- El WebView (Browser de Capacitor) hereda cookies persistentes del sistema, manteniendo la sesión mientras la cookie siga vigente en el backend.
- Si necesitas más control, puedes usar el plugin `@capacitor/http` y gestionar cookies manualmente.
