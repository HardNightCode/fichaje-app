package com.nexus.fichaje;

import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

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
      settings.setSupportMultipleWindows(false);
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
