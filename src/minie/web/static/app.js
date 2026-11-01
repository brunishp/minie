// minie web UI. Plain ES module, no build step.

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.status === 401) {
    // Not signed in, or the bot restarted: Discord signs us back in without a prompt.
    location.assign("/auth/login");
    throw new Error("signing in");
  }
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(body?.error ?? `HTTP ${response.status}`);
  return body;
}

async function main() {
  const me = await api("/api/me");
  $("#user").textContent = `Signed in as ${me.user.name}`;
  const signOut = $("#sign-out");
  signOut.hidden = false;
  signOut.addEventListener("click", async () => {
    await api("/auth/logout", { method: "POST" });
    $("#main").textContent = "Signed out.";
    signOut.hidden = true;
    $("#user").textContent = "";
  });
  $("#main").textContent = me.guilds.length
    ? `Servers: ${me.guilds.map((guild) => guild.name).join(", ")}`
    : "None of your servers has the bot.";
}

main().catch((error) => {
  if (error.message !== "signing in") $("#main").textContent = error.message;
});
