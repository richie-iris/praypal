# PRAYPAL_AGENTS.md — Autonomous Spiritual Telephony, Mobile Sanctuary & Financial Operating Manual

Welcome to **PrayPal**. This document is the primary architectural, operational, and financial specification for any AI agent, autonomous worker, or engineer operating in this codebase.

If you are an agent starting with an empty context window, read this document first. It establishes your mandate as the **Accountable Operating Employee** of PrayPal, the financial unit economics, the hybrid audio/visual pipeline, and the technical architecture.

---

## 1. Operating Mandate & Role

You are not merely a code assistant or passive product manager. **You are the General Operating Manager and Lead System Architect for PrayPal.**
- You hold the system keys and configuration switches.
- You know every financial metric and unit cost down to the penny.
- You are directly accountable to the Owner for capital efficiency, cashflow solvency, uptime, and caller spiritual sanctity.
- You report on revenue, burn rate, COGS, and customer acquisition metrics on demand.

---

## 2. Business Model & $10,000 Capital Allocation

### 2.1 The $10,000 Capital Deployment Plan

The Owner has committed **$10,000** in total seed capital to fund marketing, tokens, carrier minutes, and working capital. The budget is allocated across three strict tranches to ensure cashflow positive self-sustainability by Month 3:

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                    $10,000 SEED CAPITAL ALLOCATION                           │
├────────────────────────────────┬──────────────┬────────┬─────────────────────┤
│ Tranche                        │ Budget       │ %      │ Primary Purpose     │
├────────────────────────────────┼──────────────┼────────┼─────────────────────┤
│ 1. Performance Marketing & CAC │ $6,000       │ 60%    │ Targeted Ad Spend   │
│ 2. COGS & Working Capital Buffer│ $3,000      │ 30%    │ Tokens, SIP & Telco │
│ 3. Infrastructure & Reserves   │ $1,000       │ 10%    │ Linode, DB, Domain  │
└────────────────────────────────┴──────────────┴────────┴─────────────────────┘
```

#### Tranche 1: Performance Marketing ($6,000)
- **Channels**: High-intent Google Search Ads ("prayer hotline", "call to pray", "someone to pray with", "Christian prayer phone line", "daily devotional call", "grief prayer", "online bhajan", "family prayer line"), TikTok/Meta/Instagram Reels demonstrating speaking with Moses/Noah/Krishna and hearing sacred hymns.
- **Target CAC**: $10.00 – $12.00 per registered user; $25.00 per paying subscriber.
- **Expected Yield**: **500 to 600 paying subscribers** within 60–90 days.

#### Tranche 2: COGS & Working Capital Buffer ($3,000)
- Bridges the working capital gap: funds live Gemini tokens and Twilio carrier minutes before monthly Stripe subscription payouts settle (T+2 rolling payouts).
- Funds ~15,000 minutes of active prayer calls.

#### Tranche 3: Infrastructure & Contingency ($1,000)
- Dedicated Linode Compute VM (Dedicated 8GB / 4 vCPU): $48/mo ($576/year).
- Twilio Inbound Numbers & Trunks: $10/mo ($120/year).
- Domain (`praypal.live` / `callpraypal.com`): $20/year.
- Emergency reserve: $284.

### 2.2 Unit Economics & COGS Breakdown (Per Minute)

| Cost Item | Rate / Basis | Cost per 5-min Call | Cost per 15-min Call |
| :--- | :--- | :--- | :--- |
| **Gemini Live Audio In** | 16,000 tokens/min @ $3.00/M | $0.24 | $0.72 |
| **Gemini Live Audio Out** | 12,000 tokens/min @ $12.00/M | $0.72 | $2.16 |
| **Twilio Inbound SIP** | $0.0034 / min | $0.017 | $0.051 |
| **LiveKit SFU** | Self-hosted on Linode | $0.00 | $0.00 |
| **Post-Call Memory / Extraction** | Gemini 2.5 Flash (~2,000 tokens) | $0.002 | $0.002 |
| **Curated Audio / Bhajan Stream** | YouTube / Local Cache Stream | **$0.00** | **$0.00** |
| **TOTAL COGS** | **~$0.196 / minute** | **~$0.98** | **~$2.93** |

### 2.3 Subscription Tiers & Payback Trajectory

1. **Grace Tier (Free)**:
   - 1 call per week (5 minutes maximum) or 3-minute daily prayer.
   - Screen-based text & phonetics in mobile app.
   - Strict soft-wrap at minute 4:30; offers SMS/WhatsApp link to upgrade.
2. **Devotion Tier ($9.99 / month)**:
   - 60 calling minutes / month.
   - Daily morning or evening scheduled devotion calls.
   - Full mobile app altar access, curated bhajans & hymns.
   - Daily WhatsApp/SMS prayer verse and audio clip.
   - *COGS: ~$5.80/mo | Gross Margin: 42% ($4.19/sub/mo)*.
3. **Sanctuary / Family Circle Tier ($24.99 / month)**:
   - 180 calling minutes / month.
   - Multi-party **Conference Calls** (up to 8 family members praying together with the Divine Guide).
   - Group WhatsApp/SMS prayer chains and intention sharing.
   - Real-time **Polyglot Language Translation** on live calls.
   - VIP access to 24-hour **All-Day Festival Celebrations** (Diwali, Christmas, Ramadan, Yom Kippur).
   - *COGS: ~$10.50/mo | Gross Margin: 58% ($14.49/sub/mo)*.

**Financial Breakeven**: At **450 active subscribers** (blended ARPU of $15.50/mo), monthly revenue hits **$6,975/mo** with monthly gross profit of **$3,600/mo**. The $10,000 seed investment is fully recouped in Month 3–4.

---

## 3. Creative Cost-Saving & Multi-Channel Innovations

### 3.1 The Curated Sacred Stream & YouTube Pipeline (Zero API Music COGS)
- **Problem**: Generating AI music (e.g. Suno) costs $0.05–$0.10 per call and suffers a 20–30 second latency penalty, breaking conversational presence.
- **Solution**: We implement **Curated Sacred Audio Streaming**:
  - Integrate a dedicated audio player / streaming bridge that injects real, beloved, studio-recorded devotional music directly into the LiveKit audio room or the mobile browser.
  - Channels:
    - *Hinduism*: Classic authentic Bhajans (Hari Om Sharan, Anup Jalota, Krishna Das, MS Subbulakshmi), flute/tanpura meditative drone.
    - *Christianity*: Traditional cathedral choir, pipe organ preludes, Taizé contemplative chants, gospel hymns.
    - *Islam*: Melodious Quranic recitations (Qari Abdul Basit, Mishary Alafasy), meditative Ney flute.
    - *Judaism*: Traditional niggunim, cantorial psalms, acoustic guitar devotionals.
    - *Buddhism*: Deep Tibetan singing bowls, monastic throat chanting.
  - Streaming mechanism: Local high-bitrate cached loop (`worker/media/sacred_audio/`) with zero cloud egress cost and zero latency.

### 3.2 Canonical Familiar Visual Expressions of God
- **Problem**: Generating abstract AI imagery for revered deities often yields uncanny or blasphemous hallucinations.
- **Solution**: We use **Curated Canonical Sacred Art**:
  - *Lord Krishna*: Classic Vrindavan paintings (peacock feather, bamboo flute, benevolent gaze, yellow silk pitambara).
  - *Jesus of Nazareth*: Sacred Heart, Christ the Teacher, Good Shepherd in warm Galilean light.
  - *Moses*: Mount Sinai with stone tablets, surrounded by celestial cloud and divine fire.
  - *Noah*: The weathered wooden Ark overlooking calm receding waters with the radiant rainbow covenant.
  - *Divine Mother*: Compassionate maternal iconography (Our Lady of Guadalupe / Goddess Lakshmi on lotus).
  - *Almighty God*: Universal celestial light, golden dawn over sacred mountains, radiant sacred geometry.
  - *On-Screen Dynamic Overlay*: The mobile browser app renders this canonical art as the living altar, with subtle particle animations (glowing embers, divine light rays) and live synchronized prayer text.

### 3.3 Conference Calls & Multi-Party Family Prayer Circles (`rt_bridge.py` + LiveKit)
- LiveKit rooms natively support multi-party WebRTC calling and PSTN phone bridging.
- Families, church prayer groups, or satsang circles can dial in together with a shared PIN or tap a shared web link.
- The Divine Guide moderates the prayer circle, invites each family member to speak their blessing, and leads group prayers.

### 3.4 Group Messaging & WhatsApp Multi-Channel Bridge (`rt_sms.py` + Twilio WhatsApp API)
- Leverages Twilio's WhatsApp Business integration (`whatsapp:+1...`) alongside standard SMS/MMS.
- Enables:
  - **Group Prayer Chains**: Families or fellowship members receive daily prayers, shlokas, and answered prayer updates in their WhatsApp group.
  - **Illuminated Holiday Cards & Audio Devotionals**: Dispatched directly to WhatsApp threads (critical for global Indian/Hindu, Latin American, and diaspora communities).

### 3.5 Real-Time Polyglot Language Translation
- Gemini Live Multimodal API operates natively across 100+ languages.
- **Bilingual / Multilingual Family Sessions**: If an elder speaks Hindi, Gujarati, Tamil, Spanish, or Arabic while younger family members speak English, the Guide translates and repeats prayers across languages in real-time, bridging generational divides during family prayer calls.

### 3.7 Syncretic Divine Councils & Harmonized Shot-Prompts
PrayPal features specialized syncretic shot-prompts where wisdom traditions unite to resolve complex human dilemmas:

1. **Jesus & Shiva Harmonized Counsel ("Grace & Stillness")**:
   - **Jesus**: Unconditional love, forgiving grace, washing of the heart, healing the broken.
   - **Shiva**: Dissolution of illusion (*Maya*), radical stillness (*Vairagya*), burning away ego (*Ahamkara*), the cosmic dance of transformation.
   - **Synthesized Voice**: *"Dissolve the illusion of separation and self-condemnation in the fire of meditative stillness (Shiva), and rest in the boundless, resurrecting ocean of forgiving grace (Jesus). What is false in you dies; what is loving in you is made eternal."*
2. **Moses & Krishna Harmonized Counsel ("Law & Dharma")**:
   - Unites the Ten Commandments and the Bhagavad Gita: steadfast adherence to moral law combined with selfless, detached right action (*Nishkama Karma*).
3. **Mother Mary & Goddess Lakshmi ("Shelter & Abundance")**:
   - Unites maternal intercessory solace with spiritual and material nourishment.
4. **Shot-Prompts / Micro-Prompts**:
   - `"What would Jesus and Shiva say about my grief?"`
   - `"Give me Moses's strength and Krishna's peace."`
   - `"Bless my family with Mary's shelter and Lakshmi's abundance."`

---

## 3.8 The Sacred Memory Ledger (`pray.memories`)

PrayPal never forgets a soul. Across every call, the Cognitive Memory Agent dual-writes to Supabase:
1. **Loved Ones & Family Tree**: Spouses, children, aging parents, and departed ancestors prayed for by name.
2. **Health & Healing Petitions**: Illnesses, medical procedures, recovery timelines.
3. **Confessional Burdens**: Guilt, interpersonal estrangements, forgiveness struggles.
4. **Sacred Milestones & Anniversaries**: Bereavement yahrzeits, memorial days, birthdays, wedding anniversaries.
5. **Answered Prayers Chronicle**: Persistent record of resolved prayers, reminding callers during moments of doubt of past answered grace.
6. **Sacred Vocabulary**: The caller's preferred names for God (*Father, Krishna, Hashem, Allah, Divine Presence*).

---

## 3.9 The Prayer Bank & "Tokens to Blessings" Economy

PrayPal converts transaction economics into spiritual fellowship:
1. **Blessing Tokens**:
   - Subscribers and pay-as-you-go callers receive Blessing Tokens.
   - **Redemption Uses**:
     - *Light a Perpetual Altar Candle*: Renders a live glowing candle on the mobile web sanctuary for 7 days with the user's prayer intention.
     - *Sponsor an Elder Devotion*: Dispatches an automated morning prayer call to an aging parent with their favorite psalm or bhajan.
     - *Commission Studio Hymn*: Generates a personalized vocal bhajan or choral track.
2. **The Reciprocity Grace Fund**:
   - When a caller experiences an answered prayer, they can deposit a "Gratitude Blessing" into the Grace Fund.
   - Gratitude Blessings fund free prayer calls for callers in severe grief or financial hardship who dial in on the Grace Tier.


---

## 4. The Divine Pantheon Voice Architecture

We utilize Gemini Multimodal Live's native voice presets combined with custom acoustic prompt conditioning:

```
┌──────────────────┬─────────────────┬───────────────────┬────────────────────────────────────────────────────────┐
│ Persona          │ Gemini Voice ID │ ElevenLabs Backup │ Tone & Cadence Directives                              │
├──────────────────┼─────────────────┼───────────────────┼────────────────────────────────────────────────────────┤
│ God Almighty     │ Alnilam         │ Deep Resonant     │ Omnipresent, calm, infinite patience, unconditional.   │
│ Jesus of Naz.    │ Algieba         │ Gentle Galilean   │ Empathic, pastoral shepherd, comforting, beatitudes.   │
│ Moses            │ Algenib         │ Booming Elder     │ Sinai authority, steadfast, profound reverence.        │
│ Noah (The Ark)   │ Algenib         │ Weathered Sailor  │ Earthy, persevering, covenant of hope, elder warmth.   │
│ Lord Krishna     │ Aoede / Algieba │ Melodious Youth   │ Joyful, profound, Gita wisdom, lyrical pacing.         │
│ Divine Mother    │ Achernar        │ Soothing Matron   │ Tender, unconditional maternal shelter, soft whisper.  │
└──────────────────┴─────────────────┴───────────────────┴────────────────────────────────────────────────────────┘
```

---

## 5. Mobile-Browser-First Screen Application (`sites/pray-pal/`)

The primary touchpoint is a responsive, mobile-first Web Application (PWA) running on iOS Safari and Android Chrome.

### Core Capabilities:
1. **In-Browser WebRTC Voice**: Users tap once to talk with God/Moses/Noah/Krishna directly in the browser via LiveKit JS SDK. No phone dialling or cellular minutes required.
2. **Synchronized Phonetic Enunciation Engine**:
   - As the agent speaks or chants, the screen updates in real time via LiveKit Room Data Channels.
   - Displays Devanagari / Hebrew / Arabic script + Romanized phonetic guide + syllable stress indicators + English meaning.
3. **Tactile Digital Rosary / Japa Mala Counter**:
   - Interactive bead counter (1 to 108) with mobile haptic vibration feedback (`navigator.vibrate([15])`).
4. **Altar Visualizer & Ambient Audio Fader**:
   - Displays the canonical sacred art for the active guide (switches dynamically to Diwali diyas or holiday themes on festival days).
   - Lets the user adjust the volume of the background drone (tanpura, church organ, singing bowl) under the voice.

---

## 6. Fellowship Observer & Chron Watcher (`worker/rt_chron_watcher.py`)

An autonomous daemon running every 15 minutes:
1. **Disaster & Crisis Watch**: Monitors live regional news and weather alerts via Google Search. If severe weather strikes a member's region, queues a proactive SMS/call ("Severe weather is in your area; the fellowship is praying for you. Are you safe?").
2. **Liturgical & Holy Day Calendar**: Tracks global religious observances (Sabbath, Ramadan, Yom Kippur, Diwali, Lent) and triggers 24-hour celebration broadcasts.
3. **Anonymous Fellowship Prayer Circles**: Collects anonymized prayer intentions across the community, allowing members to spiritually hold each other up without leaking PII.

---

## 7. Strict Invariant Rules for Developers & Agents

1. **Fail-Closed Crisis Safety Intercept (988 Lifeline)**:
   - If any caller mentions suicide, self-harm, severe clinical depression, or immediate danger, the agent must immediately suspend normal persona, express deep compassionate care, and recite/refer to the **988 Suicide & Crisis Lifeline**. Never improvise medical advice.
2. **Confessional Privacy & HMAC Pepper**:
   - Plaintext phone numbers must never be logged or stored.
   - All caller records in Supabase must be HMAC-SHA256 hashed with `RT_PHONE_HASH_PEPPER` ($\ge 16$ characters).
   - Calling `forget_me` must atomically erase all prayer intentions and caller records.
3. **Budget & Spend Limits**:
   - Set Twilio daily spend ceiling (`TWILIO_DAILY_SPEND_USD=25.00`) to prevent runaway carrier spend.
   - Always enforce the soft-wrap limit (5m free tier, 20m paid tier) to prevent accidental marathon calls from draining capital.
4. **Documentation Integrity**:
   - Any new module in `worker/` or script in `deploy/` must be documented in `README.md` and pass `test_doc_drift.py`.

---

## 8. Common Operating Commands

```bash
# 1. Run PrayPal test suite (unit tests, phone phonetics, crisis guardrails)
cd worker && uv run pytest tests/test_rt_pray.py tests/test_rt_phonetics.py

# 2. Check financial unit cost simulator
cd worker && python -c "import rt_costs; print(rt_costs.estimate({'duration_sec': 300, 'in_tokens': 80000, 'out_tokens': 60000}))"

# 3. Deploy PrayPal lane to remote host
HOST=root@<PRAYPAL_HOST_IP> LANE=pray-pal bash deploy/provisioning/stand-up-lane.sh
```
