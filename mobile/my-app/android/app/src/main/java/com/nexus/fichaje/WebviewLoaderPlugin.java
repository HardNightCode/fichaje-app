package com.nexus.fichaje;

import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebChromeClient;
import android.webkit.GeolocationPermissions;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

@CapacitorPlugin(name = "WebviewLoader")
public class WebviewLoaderPlugin extends Plugin {

  @PluginMethod
  public void loadUrl(PluginCall call) {
    String url = call.getString("url");
    if (url == null || url.trim().isEmpty()) {
      call.reject("Missing url");
      return;
    }

    getActivity().runOnUiThread(() -> {
      WebView webView = getBridge().getWebView();
      WebSettings settings = webView.getSettings();
      settings.setJavaScriptEnabled(true);
      settings.setDomStorageEnabled(true);
      settings.setDatabaseEnabled(true);
      settings.setGeolocationEnabled(true);
      settings.setSupportMultipleWindows(false);
      webView.setWebChromeClient(new WebChromeClient() {
        @Override
        public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
          callback.invoke(origin, true, false); // otorgar permisos de geolocalización al WebView
        }
      });
      webView.setWebViewClient(new WebViewClient() {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
          return false; // Siempre dentro del WebView
        }
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
          return false; // Compatibilidad
        }
      });

      webView.loadUrl(url);
      JSObject ret = new JSObject();
      ret.put("ok", true);
      call.resolve(ret);
    });
  }
}
