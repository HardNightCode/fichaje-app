package com.nexus.fichaje;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
  @Override
  public void onCreate(Bundle savedInstanceState) {
    registerPlugin(WebviewLoaderPlugin.class);
    super.onCreate(savedInstanceState);
  }
}
