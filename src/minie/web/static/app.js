// minie web UI. Plain ES module, no build step. Text from the server only ever goes into the
// page through textContent, never as HTML.

const $ = (selector) => document.querySelector(selector);

const state = {
  guildId: null,
  sounds: [],
  status: null,
  events: null,
};

// Per-browser conveniences only; the page works the same when storage is unavailable.
const saved = {
  get(key, fallback) {
    try {
      const value = localStorage.getItem(`minie.${key}`);
      return value === null ? fallback : JSON.parse(value);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`minie.${key}`, JSON.stringify(value));
    } catch {
      // private window, storage blocked: nothing to remember then
    }
  },
};

class SigningIn extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) {
    // Not signed in, or the bot restarted: Discord signs us back in without a prompt.
    location.assign("/auth/login");
    throw new SigningIn();
  }
  const answer = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(answer?.error ?? `HTTP ${response.status}`);
  return answer;
}

const guildPath = (path) => `/api/guilds/${state.guildId}${path}`;

// Where a sound plays. Only the bot exists for now; playing on this device will be another
// target with the same shape.
const targets = {
  bot: {
    async play(name) {
      const { position } = await api(guildPath("/play"), { method: "POST", body: { name } });
      return position;
    },
  },
};
let target = targets.bot;

let toastTimer;
function toast(text, { error = false } = {}) {
  const element = $("#toast");
  element.textContent = text;
  element.classList.toggle("error", error);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (element.textContent = ""), 4000);
}

async function attempt(action) {
  try {
    return await action();
  } catch (error) {
    if (!(error instanceof SigningIn)) toast(error.message, { error: true });
  }
}

async function playSound(name) {
  await attempt(async () => {
    const position = await target.play(name);
    if (position > 0) toast(`Queued ${name} (position ${position}).`);
  });
}

function renderGrid() {
  const grid = $("#grid");
  grid.replaceChildren();
  if (state.sounds.length === 0) {
    grid.textContent = "No sounds yet. Add one with /add.";
    return;
  }
  for (const sound of state.sounds) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sound";
    button.textContent = sound.name;
    button.title = `${(sound.duration_ms / 1000).toFixed(1)} s`;
    button.addEventListener("click", () => playSound(sound.name));
    grid.append(button);
  }
}

function renderStatus() {
  const status = state.status;
  let text = "Not in a voice channel";
  if (status.channel) {
    text = status.channel.name ? `In #${status.channel.name}` : "In a channel you can't see";
    if (status.playing) text += " · playing";
    if (status.queued) text += ` · ${status.queued} waiting`;
  }
  $("#status").textContent = text;
  $("#stop").disabled = !(status.with_you && (status.playing || status.queued));
  $("#leave").disabled = !status.channel;
}

async function loadSounds() {
  state.sounds = (await api(guildPath("/sounds"))).sounds;
  renderGrid();
}

async function loadStatus() {
  state.status = await api(guildPath("/status"));
  renderStatus();
}

function follow() {
  state.events?.close();
  const events = new EventSource(guildPath("/events"));
  events.addEventListener("sounds", () => attempt(loadSounds));
  events.addEventListener("status", () => attempt(loadStatus));
  events.addEventListener("open", () => attempt(() => Promise.all([loadSounds(), loadStatus()])));
  events.addEventListener("error", () => {
    // The browser retries on its own, unless the server refused the stream (signed out,
    // removed from the server): check why, then start over.
    if (events.readyState === EventSource.CLOSED) {
      setTimeout(() => attempt(async () => (await api("/api/me"), follow())), 3000);
    }
  });
  state.events = events;
}

function selectGuild(guildId) {
  state.guildId = guildId;
  saved.set("guild", guildId);
  $("#board").hidden = false;
  follow();
}

async function main() {
  const me = await api("/api/me");
  $("#user").textContent = me.user.name;
  const signOut = $("#sign-out");
  signOut.hidden = false;
  signOut.addEventListener("click", () =>
    attempt(async () => {
      await api("/auth/logout", { method: "POST", body: {} });
      state.events?.close();
      location.reload();
    }),
  );

  if (me.guilds.length === 0) {
    $("#message").textContent = "None of your servers has the bot.";
    return;
  }
  $("#message").hidden = true;
  const picker = $("#guild");
  for (const guild of me.guilds) picker.append(new Option(guild.name, guild.id));
  picker.hidden = me.guilds.length < 2;
  const remembered = saved.get("guild", null);
  picker.value = me.guilds.some((guild) => guild.id === remembered) ? remembered : me.guilds[0].id;
  picker.addEventListener("change", () => selectGuild(picker.value));

  $("#stop").addEventListener("click", () => attempt(() => api(guildPath("/stop"), { method: "POST", body: {} })));
  $("#leave").addEventListener("click", () => attempt(() => api(guildPath("/leave"), { method: "POST", body: {} })));
  selectGuild(picker.value);
}

main().catch((error) => {
  if (!(error instanceof SigningIn)) $("#message").textContent = error.message;
});
