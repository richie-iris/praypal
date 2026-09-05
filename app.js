/**
 * PRAYPAL — Interactive Sanctuary & Living Altar Application
 * Features:
 *  - Guide Switching & Syncretic Council (Jesus + Shiva)
 *  - Live Phonetic Syllable Animation (Sanskrit, Hebrew, Arabic, English)
 *  - Tactile Haptic Japa Mala Counter (Web Vibration API + 108 Bead Completion)
 *  - Zero-Latency Web Audio Synthesizer (Harmonic Sacred Soundscapes)
 *  - In-Browser Web Voice Session Simulation
 */

// ── 1. SACRED DATA DICTIONARIES ───────────────────────────────────────────

const GUIDES = {
  god: {
    name: "God Almighty (Loving Presence)",
    icon: "🕊️",
    motto: '"I am with you always, even unto the end of the world."',
    theme: "universal",
    tradition: "sanskrit"
  },
  jesus: {
    name: "Jesus of Nazareth (The Good Shepherd)",
    icon: "✝️",
    motto: '"Come to me, all who labor and are heavy laden, and I will give you rest."',
    theme: "christian",
    tradition: "english"
  },
  shiva: {
    name: "Lord Shiva (The Great Stillness)",
    icon: "🔱",
    motto: '"Dissolve the illusions of the mind; awaken the eternal silence within."',
    theme: "hindu",
    tradition: "sanskrit"
  },
  krishna: {
    name: "Lord Krishna (Dharma & Celestial Joy)",
    icon: "🪈",
    motto: '"Abandon all variations of anxiety and surrender unto grace. I shall deliver you."',
    theme: "hindu",
    tradition: "sanskrit"
  },
  moses: {
    name: "Moses (Sinai & The Sacred Law)",
    icon: "📜",
    motto: '"The Lord will fight for you, and you have only to be silent."',
    theme: "jewish",
    tradition: "hebrew"
  },
  noah: {
    name: "Noah (The Ark & Covenant of Hope)",
    icon: "🌈",
    motto: '"Beyond the storm, the olive branch appears. The covenant stands firm."',
    theme: "abrahamic",
    tradition: "hebrew"
  },
  mother: {
    name: "Divine Mother (Maternal Solace)",
    icon: "🌸",
    motto: '"Rest in the unconditional shelter of motherly love. You are forever safe."',
    theme: "universal",
    tradition: "sanskrit"
  },
  syncretic: {
    name: "Council of Light: Jesus & Shiva",
    icon: "✨",
    motto: '"Dissolve the illusion of separation in stillness (Shiva), and rest in boundless, forgiving grace (Jesus)."',
    theme: "syncretic",
    tradition: "sanskrit"
  }
};

const SCRIPTURES = {
  sanskrit: {
    tag: "SANSKRIT / GAYATRI MANTRA",
    original: "ॐ भूर्भुवः स्वः तत्सवितुर्वरेण्यं भर्गो देवस्य धीमहि धियो यो नः प्रचोदयात्",
    phonetic: [
      { text: "OM", note: "Deep chest resonance" },
      { text: "BHOOR", note: "Earth plane" },
      { text: "BHOO-VAH", note: "Atmospheric plane" },
      { text: "SWA-HAH", note: "Exhale • Celestial" },
      { text: "•" },
      { text: "TAT", note: "That" },
      { text: "SA-VI-TUR", note: "Divine Sun" },
      { text: "VA-REN-YAM", note: "Adorable Glory" }
    ],
    meaning: '"May the supreme divine light illuminate our minds and awaken higher awareness."'
  },
  hebrew: {
    tag: "HEBREW / SHEMA YISRAEL",
    original: "שְׁמַע יִשְׂרָאֵל יְהוָה אֱלֹהֵינוּ יְהוָה אֶחָֽד",
    phonetic: [
      { text: "SHE-MA", note: "Hear & Obey" },
      { text: "YIS-RA-EL", note: "Israel" },
      { text: "•" },
      { text: "A-DO-NAI", note: "The Lord" },
      { text: "E-LO-HEI-NU", note: "Our God" },
      { text: "•" },
      { text: "A-DO-NAI", note: "The Lord" },
      { text: "E-CHAD", note: "Is One (Sole & Unified)" }
    ],
    meaning: '"Hear, O Israel: The Lord our God, the Lord is One."'
  },
  arabic: {
    tag: "ARABIC / AL-FATIHA (THE OPENING)",
    original: "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ • الْحَمْدُ لِلَّهِ رَبِّ الْعَالَمِينَ",
    phonetic: [
      { text: "BIS-MIL-LAAH", note: "In the name of God" },
      { text: "AR-RAH-MAAN", note: "The All-Merciful" },
      { text: "AR-RA-HEEM", note: "The Especially Compassionate" },
      { text: "•" },
      { text: "AL-HAM-DU", note: "All praise belongs to" },
      { text: "LIL-LAA-HI", note: "God" },
      { text: "RAB-BIL", note: "Lord of" },
      { text: "AA-LA-MEEN", note: "The worlds" }
    ],
    meaning: '"In the name of God, the Entirely Merciful, the Especially Merciful. All praise is due to God, Lord of all creation."'
  },
  english: {
    tag: "PSALM 23 / THE LORD IS MY SHEPHERD",
    original: "The Lord is my shepherd; I shall not want. He makes me lie down in green pastures; He leads me beside still waters.",
    phonetic: [
      { text: "The", note: "" },
      { text: "LORD", note: "Anchor of Light" },
      { text: "is", note: "" },
      { text: "my", note: "" },
      { text: "SHEP-HERD", note: "Protective Guide" },
      { text: "•" },
      { text: "I", note: "" },
      { text: "SHALL", note: "" },
      { text: "NOT", note: "" },
      { text: "WANT", note: "Total Fulfillment" }
    ],
    meaning: '"Though I walk through the valley of the shadow of death, I will fear no evil: for Thou art with me."'
  }
};

// ── 2. STATE MANAGEMENT ───────────────────────────────────────────────────

let currentGuide = "god";
let currentTradition = "sanskrit";
let malaBeads = 0;
let isCalling = false;
let syllableInterval = null;

// ── 3. GUIDE & SCRIPTURE SWITCHING ────────────────────────────────────────

function selectGuide(key) {
  if (!GUIDES[key]) return;
  currentGuide = key;
  const guide = GUIDES[key];

  // Update pills UI
  document.querySelectorAll("#guide-pills .pill").forEach(pill => {
    pill.classList.remove("active");
  });
  const activeBtn = Array.from(document.querySelectorAll("#guide-pills .pill")).find(b => 
    b.getAttribute("onclick")?.includes(`'${key}'`)
  );
  if (activeBtn) activeBtn.classList.add("active");

  // Update Altar
  document.getElementById("altar-icon").textContent = guide.icon;
  document.getElementById("altar-name").textContent = guide.name;
  document.getElementById("altar-motto").textContent = guide.motto;

  // Set corresponding scripture tradition
  setScripture(guide.tradition);
}

function setScripture(lang) {
  if (!SCRIPTURES[lang]) return;
  currentTradition = lang;
  const scrip = SCRIPTURES[lang];

  // Update lang buttons
  document.querySelectorAll(".lang-btn").forEach(btn => {
    btn.classList.remove("active");
    if (btn.getAttribute("onclick")?.includes(`'${lang}'`)) {
      btn.classList.add("active");
    }
  });

  document.getElementById("scripture-tradition").textContent = scrip.tag;
  document.getElementById("scrip-orig").textContent = scrip.original;
  document.getElementById("scrip-mean").textContent = scrip.meaning;

  // Render phonetic syllables
  const phonContainer = document.getElementById("scrip-phon");
  phonContainer.innerHTML = "";
  scrip.phonetic.forEach((item, idx) => {
    if (item.text === "•") {
      const sep = document.createElement("span");
      sep.textContent = " • ";
      sep.style.color = "var(--text-dim)";
      phonContainer.appendChild(sep);
      return;
    }
    const span = document.createElement("span");
    span.className = "syllable" + (idx === 0 ? " highlight" : "");
    span.textContent = item.text;
    span.title = item.note || "";
    phonContainer.appendChild(span);
  });

  startSyllableAnimation();
}

function startSyllableAnimation() {
  if (syllableInterval) clearInterval(syllableInterval);
  let activeIdx = 0;

  syllableInterval = setInterval(() => {
    const syllables = document.querySelectorAll("#scrip-phon .syllable");
    if (!syllables.length) return;
    syllables.forEach(s => s.classList.remove("highlight"));
    activeIdx = (activeIdx + 1) % syllables.length;
    syllables[activeIdx].classList.add("highlight");
  }, 1600);
}

// ── 4. TACTILE JAPA MALA & ROSARY (HAPTIC ENGINE) ─────────────────────────

function advanceMala() {
  malaBeads = (malaBeads + 1) % 109;
  if (malaBeads === 108) {
    // 108 Completion Event!
    triggerHaptic([40, 60, 40, 60, 80]);
    alert("📿 Auspicious Completion: You have completed a full 108 Japa Mala cycle. Peace and divine blessings be upon you.");
    malaBeads = 0;
  } else {
    // Normal single bead haptic tick
    triggerHaptic([22]);
  }

  updateMalaDisplay();
}

function resetMala() {
  malaBeads = 0;
  triggerHaptic([10]);
  updateMalaDisplay();
}

function updateMalaDisplay() {
  document.getElementById("mala-count").textContent = malaBeads;
  const pct = Math.min((malaBeads / 108) * 100, 100);
  document.getElementById("mala-progress").style.width = pct + "%";
}

function triggerHaptic(pattern) {
  if ("vibrate" in navigator) {
    try {
      navigator.vibrate(pattern);
    } catch (e) {
      // Haptics not allowed without user gesture on some browsers
    }
  }
}

// Spacebar triggers Mala bead
window.addEventListener("keydown", (e) => {
  if (e.code === "Space" && e.target.tagName !== "INPUT" && e.target.tagName !== "TEXTAREA") {
    e.preventDefault();
    advanceMala();
  }
});

// ── 5. ZERO-COGS WEB AUDIO SYNTHESIZER ───────────────────────────────────

let audioCtx = null;
let masterGain = null;
let currentOscs = [];
let currentSound = null;

function initAudioContext() {
  if (!audioCtx) {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    audioCtx = new AudioContextClass();
    masterGain = audioCtx.createGain();
    masterGain.gain.setValueAtTime(0.6, audioCtx.currentTime);
    masterGain.connect(audioCtx.destination);
  }
  if (audioCtx.state === "suspended") {
    audioCtx.resume();
  }
}

function stopAmbient() {
  currentOscs.forEach(node => {
    try {
      node.stop();
      node.disconnect();
    } catch (e) {}
  });
  currentOscs = [];
  currentSound = null;
  document.querySelectorAll(".audio-btn").forEach(b => b.classList.remove("active"));
}

function playAmbient(type) {
  initAudioContext();

  if (currentSound === type) {
    stopAmbient();
    return;
  }

  stopAmbient();
  currentSound = type;

  // Highlight active button
  document.querySelectorAll(".audio-btn").forEach(b => {
    if (b.getAttribute("onclick")?.includes(`'${type}'`)) {
      b.classList.add("active");
    }
  });

  const now = audioCtx.currentTime;

  if (type === "flute") {
    // Tanpura drone in C# (138.59 Hz) with 5th overtone and gentle flutter
    createDrone(138.59, "sawtooth", 0.15);
    createDrone(207.65, "sine", 0.12);
    createDrone(277.18, "triangle", 0.08);
  } else if (type === "organ") {
    // Cathedral sacred organ chord (C Major triad with rich warm harmonics)
    createDrone(130.81, "sawtooth", 0.12); // C3
    createDrone(164.81, "triangle", 0.10); // E3
    createDrone(196.00, "triangle", 0.10); // G3
    createDrone(261.63, "sine", 0.08);     // C4
  } else if (type === "singing_bowl") {
    // Sacred 432 Hz Tibetan singing bowl harmonic
    createBowl(432.0, 0.25);
    createBowl(864.0, 0.08);
  } else if (type === "gregorian") {
    // Gregorian monk chant low vocal drone (D2 minor 73.42 Hz)
    createDrone(73.42, "sawtooth", 0.14);
    createDrone(110.00, "triangle", 0.12);
    createDrone(146.83, "sine", 0.08);
  } else if (type === "bells") {
    // Recurring temple bell chime
    playBellLoop();
  }
}

function createDrone(freq, type, gainVal) {
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  const filter = audioCtx.createBiquadFilter();

  osc.type = type;
  osc.frequency.setValueAtTime(freq, audioCtx.currentTime);

  filter.type = "lowpass";
  filter.frequency.setValueAtTime(450, audioCtx.currentTime);

  gain.gain.setValueAtTime(gainVal, audioCtx.currentTime);

  osc.connect(filter);
  filter.connect(gain);
  gain.connect(masterGain);

  osc.start();
  currentOscs.push(osc);
}

function createBowl(freq, gainVal) {
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();

  osc.type = "sine";
  osc.frequency.setValueAtTime(freq, audioCtx.currentTime);

  // Gentle periodic frequency modulation (shimmer)
  const lfo = audioCtx.createOscillator();
  lfo.frequency.setValueAtTime(0.2, audioCtx.currentTime);
  const lfoGain = audioCtx.createGain();
  lfoGain.gain.setValueAtTime(1.5, audioCtx.currentTime);
  lfo.connect(osc.frequency);
  lfo.start();
  currentOscs.push(lfo);

  gain.gain.setValueAtTime(gainVal, audioCtx.currentTime);

  osc.connect(gain);
  gain.connect(masterGain);

  osc.start();
  currentOscs.push(osc);
}

function playBellLoop() {
  createDrone(174.61, "sine", 0.1); // Soft root drone
  const strikeBell = () => {
    if (currentSound !== "bells") return;
    const bell = audioCtx.createOscillator();
    const bellGain = audioCtx.createGain();
    bell.type = "triangle";
    bell.frequency.setValueAtTime(880, audioCtx.currentTime);
    bellGain.gain.setValueAtTime(0.3, audioCtx.currentTime);
    bellGain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 3.5);

    bell.connect(bellGain);
    bellGain.connect(masterGain);

    bell.start();
    bell.stop(audioCtx.currentTime + 3.6);
  };
  strikeBell();
  const interval = setInterval(() => {
    if (currentSound !== "bells") {
      clearInterval(interval);
      return;
    }
    strikeBell();
  }, 4500);
}

function setVolume(val) {
  document.getElementById("vol-display").textContent = val + "%";
  if (masterGain && audioCtx) {
    masterGain.gain.setValueAtTime(val / 100, audioCtx.currentTime);
  }
}

// ── 6. IN-BROWSER WEBRTC VOICE SESSION ────────────────────────────────────

function startWebSession() {
  const btn = document.getElementById("webrtc-call-btn");
  const wave = document.getElementById("waveform");

  if (isCalling) {
    // End session
    isCalling = false;
    btn.innerHTML = `
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
      Talk in Browser
    `;
    btn.classList.remove("btn-primary");
    btn.classList.add("btn-secondary");
    wave.classList.remove("active");
    triggerHaptic([30]);
    return;
  }

  // Request microphone & connect
  navigator.mediaDevices?.getUserMedia({ audio: true })
    .then(stream => {
      isCalling = true;
      btn.innerHTML = `
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/></svg>
        End Web Sanctuary Session
      `;
      btn.classList.remove("btn-secondary");
      btn.classList.add("btn-primary");
      wave.classList.add("active");
      triggerHaptic([50, 50]);

      // Automatically play gentle background drone
      if (!currentSound) playAmbient("flute");

      // Voice prompt simulation
      const guideName = GUIDES[currentGuide].name;
      alert(`🕊️ Connected to the Sanctuary. You are now speaking with ${guideName}. Speak your heart; God is listening.`);
    })
    .catch(err => {
      alert("Microphone permission was not granted. You can still dial the live line anytime at +1 (973) 606-9515.");
    });
}

// ── 7. STRIPE CHECKOUT MODAL ──────────────────────────────────────────────

function openCheckout(tier) {
  triggerHaptic([30]);
  const tierName = tier === "devotion" ? "Devotion Tier ($9.99/mo)" : "Sanctuary Tier ($24.99/mo)";
  const confirmed = confirm(
    `🕊️ PrayPal Checkout — ${tierName}\n\n` +
    `Proceed to secure Stripe billing to activate your monthly calling minutes, scheduled daily blessings, and the Prayer Bank?`
  );
  if (confirmed) {
    // In production, redirects to Stripe Customer Checkout session endpoint:
    // window.location.href = `/api/create-checkout-session?tier=${tier}`;
    alert(`Thank you for entering the PrayPal Fellowship. Your ${tierName} subscription will be activated upon Stripe settlement.`);
  }
}

// ── 8. INITIALIZATION ─────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  selectGuide("god");
  setScripture("sanskrit");
  updateMalaDisplay();
});
