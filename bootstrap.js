/* Zotero 7–10 bootstrap 入口：只负责生命周期，业务逻辑都在 plugin.js */

function install() {}
function uninstall() {}

async function startup({ id, version, rootURI }) {
  await Zotero.initializationPromise;
  Services.scriptloader.loadSubScript(rootURI + "plugin.js");
  Zotero.FTT = FTT;
  await FTT.onStartup({ id, version, rootURI });
}

function shutdown(_data, reason) {
  if (reason === APP_SHUTDOWN) return;
  if (typeof FTT !== "undefined") {
    FTT.onShutdown();
  }
  delete Zotero.FTT;
}

function onMainWindowLoad({ window }) {
  if (Zotero.FTT) Zotero.FTT.addToWindow(window);
}

function onMainWindowUnload({ window }) {
  if (Zotero.FTT) Zotero.FTT.removeFromWindow(window);
}
