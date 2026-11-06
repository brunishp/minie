// minie web UI. Plain ES module, no build step. Text from the server only ever goes into the
// page through textContent, never as HTML.

const $ = (selector) => document.querySelector(selector);

const state = {
  guildId: null,
  sounds: [],
  maxUploadBytes: Infinity,
  status: null,
  events: null,
  favourites: new Set(),
  armed: null, // the sound media keys fire
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

async function api(path, { method = "GET", body, form } = {}) {
  // Writes carry a JSON body, or the X-Minie header for uploads: both need a CORS preflight
  // from another site, which the server never grants.
  let headers = {};
  let payload;
  if (form !== undefined) {
    headers = { "X-Minie": "1" };
    payload = form;
  } else if (body !== undefined) {
    headers = { "Content-Type": "application/json" };
    payload = JSON.stringify(body);
  }
  const response = await fetch(path, { method, headers, body: payload });
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

// Favourites come first in the grid, and are what next/previous step through.
function ordered() {
  const favourite = (sound) => (state.favourites.has(sound.name) ? 0 : 1);
  return [...state.sounds].sort((a, b) => favourite(a) - favourite(b));
}

function cycle() {
  const favourites = state.sounds.filter((sound) => state.favourites.has(sound.name));
  return (favourites.length ? favourites : state.sounds).map((sound) => sound.name);
}

function toggleFavourite(name) {
  if (state.favourites.has(name)) state.favourites.delete(name);
  else state.favourites.add(name);
  saved.set(`favourites.${state.guildId}`, [...state.favourites]);
  renderGrid();
  mediaKeys.refresh();
}

function renderGrid() {
  const grid = $("#grid");
  grid.replaceChildren();
  if (state.sounds.length === 0) {
    grid.textContent = "No sounds yet. Add one below, or with /add in Discord.";
    return;
  }
  for (const sound of ordered()) {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.classList.toggle("armed", mediaKeys.enabled && sound.name === state.armed);

    const play = document.createElement("button");
    play.type = "button";
    play.className = "sound";
    play.textContent = sound.name;
    play.title = `${(sound.duration_ms / 1000).toFixed(1)} s`;
    play.addEventListener("click", () => playSound(sound.name));

    const star = document.createElement("button");
    star.type = "button";
    star.className = "star";
    const isFavourite = state.favourites.has(sound.name);
    star.textContent = isFavourite ? "★" : "☆";
    star.setAttribute("aria-pressed", String(isFavourite));
    star.setAttribute("aria-label", `Favourite ${sound.name}`);
    star.addEventListener("click", () => toggleFavourite(sound.name));

    tile.append(play, star);
    if (sound.can_manage) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "more";
      more.textContent = "⋯";
      more.setAttribute("aria-label", `Rename or remove ${sound.name}`);
      more.addEventListener("click", () => openManage(sound.name));
      tile.append(more);
    }
    grid.append(tile);
  }
}

function openManage(name) {
  const dialog = $("#manage");
  dialog.dataset.sound = name;
  $("#manage-title").textContent = name;
  $("#manage-name").value = name;
  dialog.showModal();
}

async function renameSound() {
  const dialog = $("#manage");
  const name = dialog.dataset.sound;
  const newName = $("#manage-name").value;
  await attempt(async () => {
    const answer = await api(guildPath(`/sounds/${encodeURIComponent(name)}`), {
      method: "PATCH",
      body: { name: newName },
    });
    if (state.favourites.delete(name)) toggleFavourite(answer.name);
    dialog.close();
    toast(`Renamed ${name} to ${answer.name}.`);
  });
}

async function removeSound() {
  const dialog = $("#manage");
  const name = dialog.dataset.sound;
  if (!confirm(`Remove ${name} for everyone on this server?`)) return;
  await attempt(async () => {
    await api(guildPath(`/sounds/${encodeURIComponent(name)}`), { method: "DELETE", body: {} });
    dialog.close();
    toast(`Removed ${name}.`);
  });
}

async function addSound(event) {
  event.preventDefault();
  const name = $("#add-name").value;
  const file = $("#add-file").files[0];
  if (!file) return;
  if (file.size > state.maxUploadBytes) {
    toast(`That file is over the ${(state.maxUploadBytes / 2 ** 20).toFixed(0)} MiB limit.`, { error: true });
    return;
  }
  const button = $("#add-form button[type=submit]");
  button.disabled = true;
  toast(`Adding ${name}…`);
  // The name goes first, so the server checks it before receiving the file.
  const form = new FormData();
  form.append("name", name);
  form.append("file", file);
  try {
    await attempt(async () => {
      const sound = await api(guildPath("/sounds"), { method: "POST", form });
      toast(`Added ${sound.name} (${(sound.duration_ms / 1000).toFixed(1)} s).`);
      $("#add-form").reset();
    });
  } finally {
    button.disabled = false;
  }
}

// Media keys, through the Media Session API. A page receives them, even in the background,
// once it is the browser's active media session: arming plays a silent clip once, from a
// click (browsers only start audio on a user gesture), then keeps it paused. Play and pause
// both fire the armed sound, since the key toggles; next and previous move the arming.
const mediaKeys = {
  enabled: false,
  audio: null,

  async enable() {
    if (!("mediaSession" in navigator)) {
      toast("This browser doesn't support media keys.", { error: true });
      return;
    }
    if (!this.audio) {
      this.audio = new Audio(URL.createObjectURL(silentWav(10)));
    }
    await this.audio.play();
    setTimeout(() => this.audio.pause(), 300);
    const handlers = {
      play: () => this.fire(),
      pause: () => this.fire(),
      nexttrack: () => this.move(1),
      previoustrack: () => this.move(-1),
      stop: () => attempt(() => api(guildPath("/stop"), { method: "POST", body: {} })),
    };
    for (const [action, handler] of Object.entries(handlers)) {
      try {
        navigator.mediaSession.setActionHandler(action, handler);
      } catch {
        // an action this browser doesn't know: the others still work
      }
    }
    this.enabled = true;
    this.refresh();
  },

  disable() {
    for (const action of ["play", "pause", "nexttrack", "previoustrack", "stop"]) {
      try {
        navigator.mediaSession.setActionHandler(action, null);
      } catch {
        // unsupported action
      }
    }
    this.audio?.pause();
    this.enabled = false;
    this.refresh();
  },

  fire() {
    navigator.mediaSession.playbackState = "paused";
    if (state.armed) playSound(state.armed);
  },

  move(step) {
    const names = cycle();
    if (names.length === 0) return;
    const index = names.indexOf(state.armed);
    state.armed = names[(index + step + names.length) % names.length];
    this.refresh();
  },

  // Keeps the armed sound valid and visible: on the page, and in the system's Now Playing
  // overlay, which shows it without leaving a game.
  refresh() {
    const names = cycle();
    if (!names.includes(state.armed)) state.armed = names[0] ?? null;
    const button = $("#media-keys");
    button.textContent = `Media keys: ${this.enabled ? "on" : "off"}`;
    button.setAttribute("aria-pressed", String(this.enabled));
    const armed = $("#armed");
    armed.hidden = !this.enabled;
    armed.textContent = state.armed ? `Armed: ${state.armed}` : "Nothing to arm";
    if (this.enabled && "mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: state.armed ?? "No sound",
        artist: "minie",
        album: $("#guild").selectedOptions[0]?.textContent ?? "",
      });
      navigator.mediaSession.playbackState = "paused";
    }
    for (const tile of document.querySelectorAll(".tile")) {
      const name = tile.querySelector(".sound").textContent;
      tile.classList.toggle("armed", this.enabled && name === state.armed);
    }
  },
};

// A silent mono WAV: enough media for the browser to hand this page the media keys.
function silentWav(seconds, rate = 8000) {
  const samples = seconds * rate;
  const view = new DataView(new ArrayBuffer(44 + samples * 2));
  const text = (offset, value) =>
    [...value].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + samples * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true); // fmt chunk size
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  text(36, "data");
  view.setUint32(40, samples * 2, true);
  return new Blob([view.buffer], { type: "audio/wav" });
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
  $("#leave").disabled = !(status.channel && status.can_leave);
}

async function loadSounds() {
  const answer = await api(guildPath("/sounds"));
  state.sounds = answer.sounds;
  state.maxUploadBytes = answer.max_upload_bytes;
  $("#name-rules").textContent = `Names: ${answer.name_rules}. Up to ${(answer.max_upload_bytes / 2 ** 20).toFixed(0)} MiB.`;
  renderGrid();
  mediaKeys.refresh();
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
  state.favourites = new Set(saved.get(`favourites.${guildId}`, []));
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
  $("#add-form").addEventListener("submit", addSound);
  $("#manage-rename").addEventListener("click", renameSound);
  $("#manage-remove").addEventListener("click", removeSound);
  $("#media-keys").addEventListener("click", () =>
    attempt(() => (mediaKeys.enabled ? mediaKeys.disable() : mediaKeys.enable())),
  );
  selectGuild(picker.value);
}

main().catch((error) => {
  if (!(error instanceof SigningIn)) $("#message").textContent = error.message;
});
