"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let csrf = "", pairing = null, concealTimer = null, downloadUrl = null;
  let pairingInFlight = false, pairingCompleted = false;
  function updatePairButton() {
    $("pair").disabled = pairingInFlight || pairingCompleted || !$("approve").checked;
  }
  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
  }
  async function request(path, body) {
    const options = {method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store"};
    if (body !== undefined) {
      options.headers = {"Content-Type": "application/json", "X-Lisa-CSRF": csrf};
      options.body = JSON.stringify(body);
    }
    const response = await fetch("/setup/lisa/" + path, options);
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || "Phone setup could not complete that step.");
    if (value.csrf) csrf = value.csrf;
    return value;
  }
  function conceal() {
    pairing = null;
    clearTimeout(concealTimer);
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    downloadUrl = null;
    $("password").value = "";
    $("endpoint").value = "";
    $("credential").hidden = true;
    $("credential-expired").hidden = false;
  }
  function showStatus(value) {
    if (value.paired) {
      pairingCompleted = true;
      updatePairButton();
      $("install").hidden = true;
      $("consent").hidden = true;
      $("connect").hidden = false;
      if (!pairing) conceal();
    }
    $("connection-status").classList.toggle("verified", value.verified === true);
    $("connection-status").textContent = value.verified === true
      ? "Your real phone report arrived. " + (value.state === "unknown" ? "Home/Away is currently unknown." : "Your current state is " + value.state + ".") + " Automatic updates still need a real outing test."
      : "Waiting for a fresh location from this paired iPhone. Send/publish once in OwnTracks, then check again.";
  }
  $("installed").addEventListener("click", () => {$("install").hidden = true; $("consent").hidden = false;});
  $("approve").addEventListener("change", updatePairButton);
  $("pair").addEventListener("click", async () => {
    if (!$("approve").checked || pairingInFlight || pairingCompleted) return;
    pairingInFlight = true;
    updatePairButton();
    try {
      pairing = await request("pair", {owner_consent: true, consenting_person: "lisa", phone_type: "iphone"});
      pairingCompleted = true;
      $("consent").hidden = true;
      $("connect").hidden = false;
      $("credential").hidden = false;
      $("credential-expired").hidden = true;
      $("endpoint").value = pairing.endpoint;
      $("password").value = pairing.password;
      concealTimer = setTimeout(conceal, 60000);
      notice("Your phone credential is ready. Save it in OwnTracks now.");
    } catch (error) {notice(error.message, true);}
    finally {
      pairingInFlight = false;
      updatePairButton();
    }
  });
  $("download").addEventListener("click", () => {
    if (!pairing) return;
    if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    downloadUrl = URL.createObjectURL(new Blob([JSON.stringify(pairing.configuration, null, 2)], {type:"application/json"}));
    const anchor = document.createElement("a");
    anchor.href = downloadUrl;
    anchor.download = "flightwall-lisa.otrc";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    notice("Open the downloaded file with OwnTracks and review it. Delete the file after import; it contains your phone credential.");
  });
  for (const [button, field] of [["copy-url", "endpoint"], ["copy-password", "password"]]) {
    $(button).addEventListener("click", async () => {
      if (!pairing) return;
      try {await navigator.clipboard.writeText($(field).value); notice("Copied. Paste it into OwnTracks.");}
      catch {$(field).focus(); $(field).select(); notice("Select and copy this value, then paste it into OwnTracks.");}
    });
  }
  $("hide-secret").addEventListener("click", conceal);
  $("check").addEventListener("click", async () => {
    $("check").disabled = true;
    try {showStatus(await request("status")); notice("Connection checked.");}
    catch (error) {notice(error.message, true);}
    finally {$("check").disabled = false;}
  });
  async function leave() {
    conceal();
    try {await request("leave", {});} catch {}
    csrf = "";
    for (const id of ["install", "consent", "connect"]) $(id).hidden = true;
    notice("Phone setup is closed. An existing OwnTracks connection keeps working. You can stop location sharing in your iPhone Settings.");
  }
  $("later").addEventListener("click", leave);
  $("close").addEventListener("click", leave);
  window.addEventListener("pagehide", conceal);
  async function start() {
    let token = new URLSearchParams(location.hash.slice(1)).get("invite");
    // Remove the fragment before any external navigation or app-store link.
    if (location.hash) history.replaceState(null, "", location.pathname);
    try {
      if (token) {await request("exchange", {token}); token = null;}
      const value = await request("status");
      notice("This invitation is just for your iPhone.");
      if (value.paired) showStatus(value);
      else $("install").hidden = false;
    } catch (error) {notice(error.message, true);}
    finally {token = null;}
  }
  start();
})();
