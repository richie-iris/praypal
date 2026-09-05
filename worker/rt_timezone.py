"""rt_timezone.py — what time it is where the caller actually is.

Everything used to run on DEFAULT_TZ, which is New York for every caller on
earth. That is right for a caller in New Jersey and five hours wrong for one in
London, and it decides two things that matter: the time she SAYS a reminder call
will come, and the quiet-hours window that decides whether a phone may ring at
all. A five-hour error there is the difference between calling someone after
breakfast and waking them at three in the morning.

The tables below were generated from Google's libphonenumber metadata
(phonenumbers 9.0.38) and then checked. They are data, not a dependency: the
library is 21.8 MB and is not needed at runtime to answer a lookup.

Three things the generation had to get right, each of which was wrong first:

  Toll-free is not a place. 800, 888, 877 and 24 other non-geographic codes
  resolve to "America/Adak" in the raw metadata, because a toll-free number has
  no location and the answer is the whole of NANP. A caller from an 800 number
  is not in the Aleutian Islands. Those codes return None.

  Eight area codes straddle two zones, and alphabetical order picks the wrong
  one every time — Alaska became Adak rather than Anchorage, Newfoundland became
  Puerto Rico. They are decided explicitly below, by where most of that area
  code's people are.

  A country with several IANA names but one clock is not ambiguous. The UK has
  four (London, Guernsey, Jersey, Isle of Man) that keep identical time, so it
  resolves to Europe/London. Australia and Russia genuinely span zones, so they
  return None rather than guess.

None means "not knowable from the number". Callers of this module fall back to
DEFAULT_TZ, and should say so rather than present a guess as fact.
"""
from __future__ import annotations



# Area code -> zone, for the North American Numbering Plan (+1).
_NANP: dict[str, str] = {
    "201": "America/New_York", "202": "America/New_York", "203": "America/New_York", "204":
    "America/Winnipeg", "205": "America/Chicago", "206": "America/Los_Angeles", "207":
    "America/New_York", "208": "America/Boise", "209": "America/Los_Angeles", "210":
    "America/Chicago", "212": "America/New_York", "213": "America/Los_Angeles", "214":
    "America/Chicago", "215": "America/New_York", "216": "America/New_York", "217":
    "America/Chicago", "218": "America/Chicago", "219": "America/New_York", "220":
    "America/New_York", "223": "America/New_York", "224": "America/Chicago", "225":
    "America/Chicago", "226": "America/Toronto", "227": "America/New_York", "228":
    "America/Chicago", "229": "America/New_York", "231": "America/New_York", "234":
    "America/New_York", "235": "America/Chicago", "236": "America/Vancouver", "239":
    "America/New_York", "240": "America/New_York", "248": "America/New_York", "249":
    "America/Toronto", "250": "America/Vancouver", "251": "America/Chicago", "252":
    "America/New_York", "253": "America/Los_Angeles", "254": "America/Chicago", "256":
    "America/Chicago", "257": "America/Vancouver", "260": "America/New_York", "262":
    "America/Chicago", "263": "America/Toronto", "267": "America/New_York", "269":
    "America/New_York", "270": "America/New_York", "272": "America/New_York", "274":
    "America/Chicago", "276": "America/New_York", "279": "America/Los_Angeles", "281":
    "America/Chicago", "283": "America/New_York", "289": "America/Toronto", "301":
    "America/New_York", "302": "America/New_York", "303": "America/Denver", "304":
    "America/New_York", "305": "America/New_York", "306": "America/Regina", "307":
    "America/Denver", "308": "America/Chicago", "309": "America/Chicago", "310":
    "America/Los_Angeles", "312": "America/Chicago", "313": "America/New_York", "314":
    "America/Chicago", "315": "America/New_York", "316": "America/Chicago", "317":
    "America/New_York", "318": "America/Chicago", "319": "America/Chicago", "320":
    "America/Chicago", "321": "America/New_York", "323": "America/Los_Angeles", "324":
    "America/New_York", "325": "America/Chicago", "326": "America/New_York", "327":
    "America/Chicago", "329": "America/New_York", "330": "America/New_York", "331":
    "America/Chicago", "332": "America/New_York", "334": "America/Chicago", "336":
    "America/New_York", "337": "America/Chicago", "339": "America/New_York", "340":
    "America/St_Thomas", "341": "America/Los_Angeles", "343": "America/Toronto", "346":
    "America/Chicago", "347": "America/New_York", "350": "America/Los_Angeles", "351":
    "America/New_York", "352": "America/New_York", "353": "America/Chicago", "354":
    "America/Toronto", "360": "America/Los_Angeles", "361": "America/Chicago", "363":
    "America/New_York", "364": "America/New_York", "365": "America/Toronto", "367":
    "America/Toronto", "368": "America/Edmonton", "369": "America/Los_Angeles", "380":
    "America/New_York", "382": "America/Toronto", "385": "America/Denver", "386":
    "America/New_York", "401": "America/New_York", "402": "America/Chicago", "403":
    "America/Edmonton", "404": "America/New_York", "405": "America/Chicago", "406":
    "America/Denver", "407": "America/New_York", "408": "America/Los_Angeles", "409":
    "America/Chicago", "410": "America/New_York", "412": "America/New_York", "413":
    "America/New_York", "414": "America/Chicago", "415": "America/Los_Angeles", "416":
    "America/Toronto", "417": "America/Chicago", "418": "America/Toronto", "419":
    "America/New_York", "423": "America/Chicago", "424": "America/Los_Angeles", "425":
    "America/Los_Angeles", "428": "America/Halifax", "430": "America/Chicago", "431":
    "America/Winnipeg", "432": "America/Chicago", "434": "America/New_York", "435":
    "America/Denver", "437": "America/Toronto", "438": "America/Toronto", "440":
    "America/New_York", "442": "America/Los_Angeles", "443": "America/New_York", "445":
    "America/New_York", "447": "America/Chicago", "448": "America/New_York", "450":
    "America/Toronto", "458": "America/Los_Angeles", "463": "America/New_York", "464":
    "America/Chicago", "468": "America/Toronto", "469": "America/Chicago", "470":
    "America/New_York", "472": "America/New_York", "474": "America/Winnipeg", "475":
    "America/New_York", "478": "America/New_York", "479": "America/Chicago", "480":
    "America/Phoenix", "484": "America/New_York", "501": "America/Chicago", "502":
    "America/New_York", "503": "America/Los_Angeles", "504": "America/Chicago", "505":
    "America/Denver", "506": "America/Halifax", "507": "America/Chicago", "508":
    "America/New_York", "509": "America/Los_Angeles", "510": "America/Los_Angeles", "512":
    "America/Chicago", "513": "America/New_York", "514": "America/Toronto", "515":
    "America/Chicago", "516": "America/New_York", "517": "America/New_York", "518":
    "America/New_York", "519": "America/Toronto", "520": "America/Phoenix", "530":
    "America/Los_Angeles", "531": "America/Chicago", "534": "America/Chicago", "539":
    "America/Chicago", "540": "America/New_York", "541": "America/Los_Angeles", "548":
    "America/Toronto", "551": "America/New_York", "557": "America/Chicago", "559":
    "America/Los_Angeles", "561": "America/New_York", "562": "America/Los_Angeles", "563":
    "America/Chicago", "564": "America/Los_Angeles", "567": "America/New_York", "570":
    "America/New_York", "571": "America/New_York", "572": "America/Chicago", "573":
    "America/Chicago", "574": "America/New_York", "575": "America/Denver", "579":
    "America/Toronto", "580": "America/Chicago", "581": "America/Toronto", "582":
    "America/New_York", "584": "America/Winnipeg", "585": "America/New_York", "586":
    "America/New_York", "587": "America/Edmonton", "601": "America/Chicago", "602":
    "America/Phoenix", "603": "America/New_York", "604": "America/Vancouver", "605":
    "America/Chicago", "606": "America/New_York", "607": "America/New_York", "608":
    "America/Chicago", "609": "America/New_York", "610": "America/New_York", "612":
    "America/Chicago", "613": "America/Toronto", "614": "America/New_York", "615":
    "America/Chicago", "616": "America/New_York", "617": "America/New_York", "618":
    "America/Chicago", "619": "America/Los_Angeles", "620": "America/Chicago", "623":
    "America/Phoenix", "626": "America/Los_Angeles", "628": "America/Los_Angeles", "629":
    "America/Chicago", "630": "America/Chicago", "631": "America/New_York", "636":
    "America/Chicago", "639": "America/Regina", "640": "America/New_York", "641":
    "America/Chicago", "645": "America/New_York", "646": "America/New_York", "647":
    "America/Toronto", "650": "America/Los_Angeles", "651": "America/Chicago", "656":
    "America/New_York", "657": "America/Los_Angeles", "658": "America/Jamaica", "659":
    "America/Chicago", "660": "America/Chicago", "661": "America/Los_Angeles", "662":
    "America/Chicago", "667": "America/New_York", "669": "America/Los_Angeles", "670":
    "Pacific/Saipan", "671": "Pacific/Guam", "672": "America/Vancouver", "678":
    "America/New_York", "680": "America/New_York", "681": "America/New_York", "682":
    "America/Chicago", "683": "America/Toronto", "686": "America/New_York", "689":
    "America/Chicago", "701": "America/Chicago", "702": "America/Los_Angeles", "703":
    "America/New_York", "704": "America/New_York", "705": "America/Toronto", "706":
    "America/New_York", "707": "America/Los_Angeles", "708": "America/Chicago", "709":
    "America/St_Johns", "712": "America/Chicago", "713": "America/Chicago", "714":
    "America/Los_Angeles", "715": "America/Chicago", "716": "America/New_York", "717":
    "America/New_York", "718": "America/New_York", "719": "America/Denver", "720":
    "America/Denver", "724": "America/New_York", "725": "America/Los_Angeles", "726":
    "America/Chicago", "727": "America/New_York", "728": "America/New_York", "730":
    "America/Chicago", "731": "America/Chicago", "732": "America/New_York", "734":
    "America/New_York", "737": "America/Chicago", "738": "America/Los_Angeles", "740":
    "America/New_York", "742": "America/Toronto", "743": "America/New_York", "747":
    "America/Los_Angeles", "748": "America/Denver", "753": "America/Toronto", "754":
    "America/New_York", "757": "America/New_York", "760": "America/Los_Angeles", "762":
    "America/New_York", "763": "America/Chicago", "765": "America/New_York", "769":
    "America/Chicago", "770": "America/New_York", "771": "America/New_York", "772":
    "America/New_York", "773": "America/Chicago", "774": "America/New_York", "775":
    "America/Los_Angeles", "778": "America/Vancouver", "779": "America/Chicago", "780":
    "America/Edmonton", "781": "America/New_York", "782": "America/Halifax", "784":
    "America/St_Vincent", "785": "America/Chicago", "786": "America/New_York", "787":
    "America/Puerto_Rico", "801": "America/Denver", "802": "America/New_York", "803":
    "America/New_York", "804": "America/New_York", "805": "America/Los_Angeles", "806":
    "America/Chicago", "807": "America/Toronto", "808": "Pacific/Honolulu", "809":
    "America/Santo_Domingo", "810": "America/New_York", "812": "America/New_York", "813":
    "America/New_York", "814": "America/New_York", "815": "America/Chicago", "816":
    "America/Chicago", "817": "America/Chicago", "818": "America/Los_Angeles", "819":
    "America/Toronto", "820": "America/Los_Angeles", "821": "America/New_York", "825":
    "America/Edmonton", "826": "America/New_York", "828": "America/New_York", "829":
    "America/Santo_Domingo", "830": "America/Chicago", "831": "America/Los_Angeles", "832":
    "America/Chicago", "835": "America/New_York", "838": "America/New_York", "839":
    "America/New_York", "840": "America/Los_Angeles", "843": "America/New_York", "845":
    "America/New_York", "847": "America/Chicago", "848": "America/New_York", "849":
    "America/Santo_Domingo", "850": "America/New_York", "854": "America/New_York", "856":
    "America/New_York", "857": "America/New_York", "858": "America/Los_Angeles", "859":
    "America/New_York", "860": "America/New_York", "862": "America/New_York", "863":
    "America/New_York", "864": "America/New_York", "865": "America/New_York", "867":
    "America/Fort_Nelson", "868": "America/Port_of_Spain", "870": "America/Chicago", "872":
    "America/Chicago", "873": "America/Toronto", "876": "America/Jamaica", "878":
    "America/New_York", "879": "America/St_Johns", "901": "America/Chicago", "902":
    "America/Halifax", "903": "America/Chicago", "904": "America/New_York", "905":
    "America/Toronto", "906": "America/New_York", "907": "America/Anchorage", "908":
    "America/New_York", "909": "America/Los_Angeles", "910": "America/New_York", "912":
    "America/New_York", "913": "America/Chicago", "914": "America/New_York", "915":
    "America/Denver", "916": "America/Los_Angeles", "917": "America/New_York", "918":
    "America/Chicago", "919": "America/New_York", "920": "America/Chicago", "925":
    "America/Los_Angeles", "928": "America/Phoenix", "929": "America/New_York", "930":
    "America/New_York", "931": "America/Chicago", "934": "America/New_York", "936":
    "America/Chicago", "937": "America/New_York", "938": "America/Chicago", "939":
    "America/Puerto_Rico", "940": "America/Chicago", "941": "America/New_York", "942":
    "America/Toronto", "943": "America/New_York", "945": "America/Chicago", "947":
    "America/New_York", "948": "America/New_York", "949": "America/Los_Angeles", "951":
    "America/Los_Angeles", "952": "America/Chicago", "954": "America/New_York", "956":
    "America/Chicago", "959": "America/New_York", "970": "America/Denver", "971":
    "America/Los_Angeles", "972": "America/Chicago", "973": "America/New_York", "975":
    "America/Chicago", "978": "America/New_York", "979": "America/Chicago", "980":
    "America/New_York", "983": "America/Denver", "984": "America/New_York", "985":
    "America/Chicago", "986": "America/Boise", "989": "America/New_York",
}

# Non-geographic +1 codes: toll-free, premium rate, personal numbers. A number
# here tells you nothing about where its owner is standing.
_NANP_NONGEO: frozenset[str] = frozenset({
    "500", "521", "522", "523", "524", "525", "526", "527", "528", "529", "532", "533", "544",
    "566", "577", "588", "600", "622", "633", "800", "833", "844", "855", "866", "877", "888",
    "900",
})

# Country calling code -> zone, only where the whole country keeps one clock.
_COUNTRY: dict[str, str] = {
    "20": "Africa/Cairo", "211": "Africa/Nairobi", "212": "Atlantic/Canary", "213":
    "Europe/Paris", "216": "Africa/Tunis", "218": "Europe/Bucharest", "220": "Africa/Banjul",
    "221": "Africa/Dakar", "222": "Africa/Nouakchott", "223": "Africa/Bamako", "224":
    "Africa/Conakry", "225": "Africa/Abidjan", "226": "Africa/Ouagadougou", "227":
    "Africa/Niamey", "228": "Africa/Lome", "229": "Africa/Porto-Novo", "230":
    "Indian/Mauritius", "231": "Atlantic/Reykjavik", "232": "Africa/Freetown", "233":
    "Africa/Accra", "234": "Africa/Lagos", "235": "Africa/Ndjamena", "236": "Africa/Bangui",
    "237": "Africa/Douala", "238": "Atlantic/Cape_Verde", "239": "Africa/Sao_Tome", "240":
    "Africa/Malabo", "241": "Africa/Libreville", "242": "Africa/Brazzaville", "243":
    "Africa/Kinshasa", "244": "Africa/Luanda", "245": "Atlantic/Reykjavik", "246":
    "Indian/Chagos", "247": "Atlantic/St_Helena", "248": "Indian/Mahe", "249":
    "Africa/Khartoum", "250": "Africa/Kigali", "251": "Africa/Addis_Ababa", "252":
    "Africa/Mogadishu", "253": "Africa/Djibouti", "254": "Africa/Nairobi", "255":
    "Africa/Dar_es_Salaam", "256": "Africa/Kampala", "257": "Africa/Bujumbura", "258":
    "Africa/Maputo", "260": "Africa/Lusaka", "261": "Indian/Antananarivo", "263":
    "Africa/Harare", "264": "Africa/Windhoek", "265": "Africa/Blantyre", "266": "Africa/Maseru",
    "267": "Africa/Gaborone", "268": "Africa/Mbabane", "269": "Indian/Comoro", "27":
    "Africa/Johannesburg", "290": "Atlantic/St_Helena", "291": "Africa/Asmera", "297":
    "America/Aruba", "298": "Atlantic/Faeroe", "299": "America/Godthab", "30": "Europe/Athens",
    "31": "Europe/Amsterdam", "32": "Europe/Brussels", "33": "Europe/Paris", "34":
    "Europe/Madrid", "350": "Europe/Gibraltar", "351": "Europe/Lisbon", "352":
    "Europe/Luxembourg", "353": "Europe/Guernsey", "354": "Atlantic/Reykjavik", "355":
    "Europe/Tirane", "356": "Europe/Malta", "357": "Asia/Nicosia", "358": "Europe/Helsinki",
    "359": "Europe/Sofia", "36": "Europe/Budapest", "370": "Europe/Bucharest", "371":
    "Europe/Bucharest", "372": "Europe/Bucharest", "373": "Europe/Chisinau", "374":
    "Asia/Yerevan", "375": "Europe/Moscow", "376": "Europe/Andorra", "377": "Europe/Monaco",
    "378": "Europe/San_Marino", "380": "Europe/Kyiv", "381": "Europe/Belgrade", "382":
    "Europe/Podgorica", "383": "Europe/Belgrade", "385": "Europe/Zagreb", "386":
    "Europe/Ljubljana", "387": "Europe/Sarajevo", "389": "Europe/Skopje", "39": "Europe/Rome",
    "40": "Europe/Bucharest", "41": "Europe/Zurich", "420": "Europe/Prague", "421":
    "Europe/Bratislava", "423": "Europe/Vaduz", "43": "Europe/Vienna", "44": "Europe/London",
    "45": "Europe/Copenhagen", "46": "Europe/Stockholm", "47": "Europe/Oslo", "48":
    "Europe/Warsaw", "49": "Europe/Berlin", "500": "Atlantic/Stanley", "501": "America/Belize",
    "502": "America/Guatemala", "503": "America/El_Salvador", "504": "America/Tegucigalpa",
    "505": "America/Chicago", "506": "America/Costa_Rica", "507": "America/Panama", "508":
    "America/Miquelon", "509": "America/Port-au-Prince", "51": "America/Lima", "53":
    "America/Havana", "54": "America/Buenos_Aires", "55": "America/Sao_Paulo", "56":
    "America/Santiago", "57": "America/Bogota", "58": "America/Caracas", "590":
    "America/Guadeloupe", "591": "America/La_Paz", "592": "America/Guyana", "593":
    "America/Guayaquil", "594": "America/Cayenne", "595": "America/Asuncion", "596":
    "America/Martinique", "597": "America/Paramaribo", "598": "America/Montevideo", "599":
    "America/Curacao", "60": "Asia/Kuching", "62": "Asia/Jakarta", "63": "Asia/Manila", "64":
    "Pacific/Auckland", "65": "Asia/Singapore", "66": "Asia/Bangkok", "670": "Asia/Dili", "672":
    "Pacific/Norfolk", "673": "Asia/Brunei", "674": "Pacific/Nauru", "675":
    "Pacific/Port_Moresby", "676": "Pacific/Tongatapu", "677": "Pacific/Guadalcanal", "678":
    "Pacific/Efate", "679": "Pacific/Fiji", "680": "Pacific/Palau", "681": "Pacific/Wallis",
    "682": "Pacific/Rarotonga", "683": "Pacific/Niue", "685": "Pacific/Apia", "687":
    "Pacific/Noumea", "688": "Pacific/Funafuti", "689": "Pacific/Tahiti", "690":
    "Pacific/Fakaofo", "691": "Pacific/Ponape", "692": "Pacific/Majuro", "81": "Asia/Tokyo",
    "82": "Asia/Seoul", "84": "Asia/Ho_Chi_Minh", "850": "Asia/Seoul", "852": "Asia/Hong_Kong",
    "853": "Asia/Shanghai", "855": "Asia/Phnom_Penh", "856": "Asia/Vientiane", "86":
    "Asia/Shanghai", "880": "Asia/Dhaka", "886": "Asia/Taipei", "90": "Europe/Istanbul", "91":
    "Asia/Calcutta", "92": "Asia/Karachi", "93": "Asia/Kabul", "94": "Asia/Colombo", "95":
    "Asia/Rangoon", "960": "Indian/Maldives", "961": "Asia/Beirut", "962": "Asia/Amman", "963":
    "Asia/Damascus", "964": "Asia/Baghdad", "965": "Asia/Kuwait", "966": "Asia/Riyadh", "967":
    "Asia/Aden", "968": "Asia/Muscat", "970": "Europe/Bucharest", "971": "Asia/Dubai", "972":
    "Asia/Jerusalem", "973": "Asia/Bahrain", "974": "Asia/Qatar", "975": "Asia/Thimphu", "976":
    "Asia/Ulaanbaatar", "977": "Asia/Katmandu", "98": "Asia/Tehran", "992": "Asia/Dushanbe",
    "993": "Asia/Ashgabat", "994": "Asia/Baku", "995": "Asia/Tbilisi", "996": "Asia/Bishkek",
    "998": "Asia/Tashkent",
}


def zone_for_number(e164: str | None) -> str | None:
    """The caller's IANA timezone, or None when the number cannot tell us.

    None is a real answer and must not be treated as "assume the server's zone
    is fine". It means the caller is somewhere unknown — a toll-free line, a
    country that spans zones, a number we cannot parse — and anything that turns
    a clock into a decision should be conservative about it.
    """
    n = (e164 or "").strip()
    if not n.startswith("+"):
        return None
    digits = "".join(c for c in n[1:] if c.isdigit())
    if not digits:
        return None
    if digits.startswith("1"):
        npa = digits[1:4]
        if len(npa) < 3:
            return None
        if npa in _NANP_NONGEO:
            return None
        return _NANP.get(npa)
    # Country codes are one to three digits and are prefix-free, so the longest
    # match that exists is the right one.
    for size in (3, 2, 1):
        hit = _COUNTRY.get(digits[:size])
        if hit:
            return hit
    return None


def zone_or_default(e164: str | None, default: str) -> tuple[str, bool]:
    """(zone, known). `known` is False when the zone is a fallback, not a fact."""
    z = zone_for_number(e164)
    if z:
        return z, True
    return default, False
