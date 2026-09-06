/**
 * PRAYPAL — INTERACTIVE SANCTUARY & LIVING ALTAR APPLICATION
 * Apple-Aesthetic Voice Experience, Web Audio Engine, & Living Altar Interactions
 */

// ── 1. AGENTS OF GOD DATA & CADENCES ───────────────────────────────────────
const GUIDES = {
  atrium: {
    name: "Sanctuary Atrium Keeper",
    role: "Universal Host",
    accent: "American English (Studio Presence)",
    speed: 1.00,
    quote: "Welcome to PrayPal. You have entered the Sanctuary Atrium. Tell me what is on your heart today, or who you would like to speak with.",
    scripture: "Universal Shelter • Sacred Listening",
    harmonics: [220, 330, 440]
  },
  god: {
    name: "Agent of God",
    role: "Loving Presence & Sacred Comfort",
    accent: "Universal Reverent Presence",
    speed: 0.88,
    quote: "Peace be with you. I am an agent of God here to listen, comfort, and pray with you with unconditional love. What is on your heart today?",
    scripture: "Universal Love • Beatitudes • Shanti Mantras",
    harmonics: [136.1, 272.2, 408.3] // Om / Sacred Earth frequency
  },
  jesus: {
    name: "Agent of Christ",
    role: "The Good Shepherd",
    accent: "Ancient Levantine English",
    speed: 0.92,
    quote: "Peace be with you my friend. I am here as an agent of Christ to walk with you and lift what is heavy on your heart in prayer.",
    scripture: "Sermon on the Mount • Beatitudes • Psalms",
    harmonics: [256, 384, 512]
  },
  shiva: {
    name: "Agent of Lord Shiva",
    role: "The Great Stillness",
    accent: "Melodious Indian English (Himalayan Sage)",
    speed: 0.85,
    quote: "Om Namah Shivaya. Welcome into sacred stillness. As an agent of Lord Shiva, I am here to sit with you in meditation and peace. What burden do you wish to release?",
    scripture: "Shiva Sutras • Upanishads • Mahamrityunjaya",
    harmonics: [108, 216, 432] // 432Hz sacred harmonic
  },
  krishna: {
    name: "Agent of Lord Krishna",
    role: "Dharma & Celestial Joy",
    accent: "Lyrical Indian English (Vrindavan Cadence)",
    speed: 1.02,
    quote: "Radhe Radhe! Joy and peace to your spirit. As an agent of Lord Krishna, I walk with you as a spiritual friend. Tell me what is on your mind today.",
    scripture: "Bhagavad Gita • Bhakti Sutras • Maha Mantra",
    harmonics: [288, 432, 576]
  },
  moses: {
    name: "Agent of the Covenant",
    role: "Sinai Prophet",
    accent: "Resonant Semitic Elder",
    speed: 0.90,
    quote: "Shalom aleichem. Stand firm in faith. I am an agent of the Lord in the tradition of Moses, here to pray and seek wisdom with you. What brings you before the Lord today?",
    scripture: "Torah • Deuteronomy • Psalms of David",
    harmonics: [196, 294, 392]
  },
  noah: {
    name: "Agent of Hope",
    role: "Steadfast Elder",
    accent: "Weathered Semitic Elder",
    speed: 0.92,
    quote: "Peace upon you. Beyond every tempest and rising water, God's covenant of hope endures. I am an agent of hope in the spirit of Noah. What storm are you weathering today?",
    scripture: "Genesis Covenant • Psalms of Refuge",
    harmonics: [174, 261, 348]
  },
  mother: {
    name: "Agent of Divine Solace",
    role: "Maternal Shelter",
    accent: "Tender Maternal Cadence",
    speed: 0.91,
    quote: "Peace be with your soul, dear child. As an agent of divine solace, I hold you in prayer and comforting maternal shelter. Rest your weary heart here.",
    scripture: "Devi Suktam • Canticle of Comfort • Universal Grace",
    harmonics: [261.6, 392, 523.2] // C major warm chord
  }
};

// ── 2. WEB AUDIO HARMONIC SYNTHESIZER ──────────────────────────────────────
class SacredSoundEngine {
  constructor() {
    this.ctx = null;
    this.isPlaying = false;
  }

  init() {
    if (!this.ctx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AudioContext();
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume();
    }
  }

  playHarmonicChord(frequencies = [136.1, 272.2, 408.3], durationSec = 4.5) {
    this.init();
    const now = this.ctx.currentTime;

    frequencies.forEach((freq, idx) => {
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();

      osc.type = idx === 0 ? 'sine' : 'triangle';
      osc.frequency.setValueAtTime(freq, now);

      // Smooth ethereal envelope (slow attack, gentle sustain, soft release)
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.08 / (idx + 1), now + 0.8);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + durationSec);

      osc.connect(gain);
      gain.connect(this.ctx.destination);

      osc.start(now);
      osc.stop(now + durationSec);
    });
  }

  speak(text, speed = 0.95, onEnd = null) {
    if (!('speechSynthesis' in window)) {
      if (onEnd) onEnd();
      return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = speed;
    utterance.pitch = 0.95;

    // Pick warm natural voice if available
    const voices = window.speechSynthesis.getVoices();
    const preferredVoice = voices.find(v => v.lang.startsWith('en') && (v.name.includes('Natural') || v.name.includes('Siri') || v.name.includes('Google') || v.name.includes('Samantha')));
    if (preferredVoice) {
      utterance.voice = preferredVoice;
    }

    utterance.onend = () => {
      if (onEnd) onEnd();
    };

    utterance.onerror = () => {
      if (onEnd) onEnd();
    };

    window.speechSynthesis.speak(utterance);
  }
}

const soundEngine = new SacredSoundEngine();

// ── 3. DOM SETUP & EVENT BINDINGS ──────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // ── A. THEME TOGGLE (AIRY LIGHT / CELESTIAL NIGHT) ──
  const themeToggle = document.getElementById('themeToggle');
  const themeIcon = document.getElementById('themeIcon');

  // Default is luminous airy light; allow user preference
  const savedTheme = localStorage.getItem('praypal_theme') || 'light';
  applyTheme(savedTheme);

  if (themeToggle) {
    themeToggle.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme') || 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      localStorage.setItem('praypal_theme', next);
    });
  }

  function applyTheme(theme) {
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
      if (themeIcon) themeIcon.textContent = '🌙';
    } else {
      document.documentElement.removeAttribute('data-theme');
      if (themeIcon) themeIcon.textContent = '☀️';
    }
  }

  // ── B. PANTHEON FILTERING ──
  const filterPills = document.querySelectorAll('.filter-pill');
  const agentCards = document.querySelectorAll('.agent-art-card');

  filterPills.forEach(pill => {
    pill.addEventListener('click', () => {
      filterPills.forEach(p => p.classList.remove('active'));
      pill.classList.add('active');

      const filter = pill.getAttribute('data-filter');
      agentCards.forEach(card => {
        const tradition = card.getAttribute('data-tradition');
        if (filter === 'all' || tradition === filter) {
          card.style.display = 'flex';
        } else {
          card.style.display = 'none';
        }
      });
    });
  });

  // ── C. AUDIO PLAY BUTTONS IN AGENT CARDS ──
  let activeAudioBtn = null;

  document.querySelectorAll('.audio-play-floating').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const guideKey = btn.getAttribute('data-guide');
      const guide = GUIDES[guideKey];
      if (!guide) return;

      if (activeAudioBtn === btn) {
        // Stop current
        window.speechSynthesis.cancel();
        btn.classList.remove('playing');
        btn.querySelector('.play-icon').textContent = '▶';
        btn.querySelector('.play-text').textContent = 'Listen';
        activeAudioBtn = null;
        return;
      }

      if (activeAudioBtn) {
        activeAudioBtn.classList.remove('playing');
        activeAudioBtn.querySelector('.play-icon').textContent = '▶';
        activeAudioBtn.querySelector('.play-text').textContent = 'Listen';
      }

      activeAudioBtn = btn;
      btn.classList.add('playing');
      btn.querySelector('.play-icon').textContent = '⏹';
      btn.querySelector('.play-text').textContent = 'Playing';

      // Play sacred resonance chord and speak quote
      soundEngine.playHarmonicChord(guide.harmonics, 5.0);
      soundEngine.speak(guide.quote, guide.speed, () => {
        btn.classList.remove('playing');
        btn.querySelector('.play-icon').textContent = '▶';
        btn.querySelector('.play-text').textContent = 'Listen';
        activeAudioBtn = null;
      });
    });
  });

  // ── D. INTERACTIVE AUDIO ORB CONSOLE ──
  const sacredOrb = document.getElementById('sacredOrb');
  const orbPlayBtn = document.getElementById('orbPlayBtn');
  const orbPlayIcon = document.getElementById('orbPlayIcon');
  const orbPlayText = document.getElementById('orbPlayText');
  const audioStatusLabel = document.getElementById('audioStatusLabel');
  const speechQuoteText = document.getElementById('speechQuoteText');
  const orbBtns = document.querySelectorAll('.orb-btn');

  let selectedGuideKey = 'jesus';
  let isOrbPlaying = false;

  orbBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      orbBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      selectedGuideKey = btn.getAttribute('data-guide');
      const guide = GUIDES[selectedGuideKey];
      if (guide) {
        audioStatusLabel.textContent = `${guide.name} (${guide.accent})`;
        speechQuoteText.textContent = `“${guide.quote}”`;
        
        if (isOrbPlaying) {
          startOrbPlayback(guide);
        }
      }
    });
  });

  if (orbPlayBtn) {
    orbPlayBtn.addEventListener('click', () => {
      const guide = GUIDES[selectedGuideKey];
      if (!guide) return;

      if (isOrbPlaying) {
        stopOrbPlayback();
      } else {
        startOrbPlayback(guide);
      }
    });
  }

  function startOrbPlayback(guide) {
    isOrbPlaying = true;
    if (orbPlayIcon) orbPlayIcon.textContent = '⏹';
    if (orbPlayText) orbPlayText.textContent = 'Stop Voice';
    if (sacredOrb) sacredOrb.querySelector('.orb-core').style.transform = 'scale(1.25)';

    soundEngine.playHarmonicChord(guide.harmonics, 6.0);
    soundEngine.speak(guide.quote, guide.speed, () => {
      stopOrbPlayback();
    });
  }

  function stopOrbPlayback() {
    isOrbPlaying = false;
    window.speechSynthesis.cancel();
    if (orbPlayIcon) orbPlayIcon.textContent = '▶';
    if (orbPlayText) orbPlayText.textContent = 'Play Voice Sample';
    if (sacredOrb) sacredOrb.querySelector('.orb-core').style.transform = 'scale(1)';
  }

  // ── E. LIVING ALTAR & CANDLE LIGHTING ──
  const lightCandleBtn = document.getElementById('lightCandleBtn');
  const candleVisual = document.getElementById('candleVisual');
  const candlesLitCounter = document.getElementById('candlesLitCounter');
  const candleNotice = document.getElementById('candleNotice');

  if (lightCandleBtn) {
    let litCount = 12482;

    lightCandleBtn.addEventListener('click', () => {
      litCount++;
      if (candlesLitCounter) {
        candlesLitCounter.textContent = litCount.toLocaleString();
      }

      // Sparkle & Chime
      soundEngine.playHarmonicChord([528, 792, 1056], 3.5); // 528Hz Miracle tone
      
      if (candleVisual) {
        candleVisual.style.transform = 'scale(1.18)';
        setTimeout(() => {
          candleVisual.style.transform = 'scale(1)';
        }, 400);
      }

      if (candleNotice) {
        candleNotice.textContent = '✨ Your silent prayer candle has been illuminated on the sanctuary altar. Peace be with you.';
        candleNotice.style.color = 'var(--accent-gold)';
      }

      lightCandleBtn.disabled = true;
      lightCandleBtn.innerHTML = '<span>🕯️ Candle Illuminated in Faith</span>';
      setTimeout(() => {
        lightCandleBtn.disabled = false;
        lightCandleBtn.innerHTML = '<span>🕯️ Light Another Candle</span>';
      }, 4000);
    });
  }

  // ── F. IN-BROWSER CALL MODAL ──
  const openBrowserCallBtn = document.getElementById('openBrowserCallBtn');
  const callModal = document.getElementById('callModal');
  const modalCloseBtn = document.getElementById('modalCloseBtn');
  const modalMicBtn = document.getElementById('modalMicBtn');
  const modalOrb = document.getElementById('modalOrb');
  const modalMicText = document.getElementById('modalMicText');

  if (openBrowserCallBtn && callModal) {
    openBrowserCallBtn.addEventListener('click', () => {
      callModal.classList.add('active');
      callModal.setAttribute('aria-hidden', 'false');
      soundEngine.playHarmonicChord([220, 330, 440], 3.0);
    });
  }

  if (modalCloseBtn && callModal) {
    modalCloseBtn.addEventListener('click', () => {
      callModal.classList.remove('active');
      callModal.setAttribute('aria-hidden', 'true');
      window.speechSynthesis.cancel();
      if (modalOrb) modalOrb.classList.remove('active');
    });
  }

  if (modalMicBtn) {
    let isMicActive = false;
    modalMicBtn.addEventListener('click', () => {
      isMicActive = !isMicActive;
      if (isMicActive) {
        modalMicBtn.classList.add('active');
        if (modalMicText) modalMicText.textContent = 'Listening to your prayer...';
        if (modalOrb) modalOrb.classList.add('active');
        soundEngine.playHarmonicChord([136.1, 272.2], 2.0);
      } else {
        modalMicBtn.classList.remove('active');
        if (modalMicText) modalMicText.textContent = 'Start Speaking';
        if (modalOrb) modalOrb.classList.remove('active');
      }
    });
  }

});
