// RVR-Wordle Word List - RVGL/Re-Volt themed words
// Words are 5-8 letters, uppercase, no spaces

const WORDS = [
  // Track names (from Re-Volt/RVGL)
  "TOYINTHEHOOD",
  "TOY2",
  "TOY3",
  "TOYWORLD",
  "TOYBOX",
  "SUPERMAR"
  
];

// Valid guesses include solution words plus common English words
// For simplicity, we'll accept any 5-8 letter word that's valid
const VALID_GUESSES = new Set([
  ...WORDS,
  // Common English words that might be guessed
  "ABOUT", "ABOVE", "ABUSE", "ACTOR", "ACUTE", "ADMIT", "ADOPT", "ADULT",
  "AFTER", "AGAIN", "AGENT", "AGREE", "AHEAD", "ALARM", "ALBUM", "ALERT",
  "ALIKE", "ALIVE", "ALLOW", "ALONE", "ALONG", "ALTER", "AMONG", "ANGER",
  "ANGLE", "ANGRY", "APART", "APPLE", "APPLY", "ARENA", "ARGUE", "ARISE",
  "ARRAY", "ASIDE", "ASSET", "AUDIO", "AUDIT", "AVOID", "AWARD", "AWARE",
  "BADLY", "BAKER", "BASIS", "BEACH", "BEGAN", "BEGIN", "BEGUN", "BEING",
  "BELOW", "BENCH", "BILLY", "BIRTH", "BLACK", "BLAME", "BLIND", "BLOCK",
  "BLOOD", "BOARD", "BRAIN", "BRAND", "BREAD", "BREAK", "BREED", "BRIEF",
  "BRING", "BROAD", "BROWN", "BUILD", "BUILT", "BUYER", "CABLE", "CALIF",
  "CARRY", "CATCH", "CAUSE", "CHAIN", "CHAIR", "CHART", "CHASE", "CHECK",
  "CHEST", "CHIEF", "CHILD", "CHINA", "CHOSE", "CIVIL", "CLAIM", "CLASS",
  "CLEAN", "CLEAR", "CLICK", "CLOCK", "CLOSE", "COACH", "COAST", "COULD",
  "COUNT", "COURT", "COVER", "CRAFT", "CRASH", "CREAM", "CRIME", "CROSS",
  "CROWD", "CROWN", "CURVE", "CYCLE", "DAILY", "DANCE", "DATED", "DEALT",
  "DEATH", "DEBUT", "DELAY", "DEPTH", "DERBY", "DOING", "DOUBT", "DOZEN",
  "DRAFT", "DRAMA", "DREAM", "DRESS", "DRIVE", "DROVE", "DYING", "EARLY",
  "EARTH", "EBONY", "ELITE", "EMPTY", "ENEMY", "ENJOY", "ENTER", "ENTRY",
  "EQUAL", "ERROR", "EVENT", "EVERY", "EXACT", "EXIST", "EXTRA", "FAITH",
  "FALSE", "FAULT", "FIBER", "FIELD", "FIFTH", "FIFTY", "FIGHT", "FINAL",
  "FIRST", "FIXED", "FLASH", "FLEET", "FLOOR", "FLUID", "FOCUS", "FORCE",
  "FORTH", "FORTY", "FORUM", "FOUND", "FRAME", "FRANK", "FRAUD", "FRESH",
  "FRONT", "FRUIT", "FULLY", "FUNNY", "GIANT", "GIVEN", "GLASS", "GLOBE",
  "GOING", "GRACE", "GRADE", "GRAND", "GRANT", "GRASS", "GREAT", "GREEN",
  "GROSS", "GROUP", "GROWN", "GUARD", "GUESS", "GUEST", "GUIDE", "HAPPY",
  "HARRY", "HEART", "HEAVY", "HENCE", "HENRY", "HORSE", "HOTEL", "HOUSE",
  "HUMAN", "IDEAL", "IMAGE", "INDEX", "INNER", "INPUT", "ISSUE", "JAPAN",
  "JIMMY", "JOINT", "JONES", "JUDGE", "KNOWN", "LABEL", "LARGE", "LASER",
  "LATER", "LAUGH", "LAYER", "LEARN", "LEASE", "LEAST", "LEAVE", "LEGAL",
  "LEVEL", "LEWIS", "LIGHT", "LIMIT", "LINKS", "LIVES", "LOCAL", "LOGIC",
  "LOOSE", "LOWER", "LUCKY", "LUNCH", "LYING", "MAGIC", "MAJOR", "MAKER",
  "MARCH", "MARIA", "MATCH", "MAYBE", "MAYOR", "MEANT", "MEDIA", "METRO",
  "MIGHT", "MINOR", "MINUS", "MIXED", "MODEL", "MONEY", "MONTH", "MORAL",
  "MOTOR", "MOUNT", "MOUSE", "MOUTH", "MOVIE", "MUSIC", "NEEDS", "NEVER",
  "NEWLY", "NIGHT", "NOISE", "NORTH", "NOTED", "NOVEL", "NURSE", "OCCUR",
  "OCEAN", "OFFER", "OFTEN", "ORDER", "OTHER", "OUGHT", "PAINT", "PANEL",
  "PAPER", "PARTY", "PATCH", "PAUSE", "PEACE", "PHASE", "PHONE", "PHOTO",
  "PIECE", "PILOT", "PITCH", "PLACE", "PLAIN", "PLANE", "PLANT", "PLATE",
  "POINT", "POUND", "POWER", "PRESS", "PRICE", "PRIDE", "PRIME", "PRINT",
  "PRIOR", "PRIZE", "PROOF", "PROUD", "PROVE", "QUEEN", "QUICK", "QUIET",
  "QUITE", "RADIO", "RAISE", "RANGE", "RAPID", "RATIO", "REACH", "READY",
  "REFER", "RIGHT", "RIVAL", "RIVER", "ROBIN", "ROBOT", "ROUND", "ROUTE",
  "ROYAL", "RURAL", "SCALE", "SCENE", "SCOPE", "SCORE", "SENSE", "SERVE",
  "SEVEN", "SHALL", "SHAPE", "SHARE", "SHARP", "SHEET", "SHELF", "SHELL",
  "SHIFT", "SHIRT", "SHOCK", "SHOOT", "SHORT", "SHOWN", "SIGHT", "SILVER",
  "SIMPLE", "SINCE", "SIXTH", "SIXTY", "SIZED", "SKILL", "SLEEP", "SLIDE",
  "SMALL", "SMART", "SMILE", "SMITH", "SMOKE", "SOLID", "SOLVE", "SORRY",
  "SOUND", "SOUTH", "SPACE", "SPARE", "SPEAK", "SPEED", "SPEND", "SPENT",
  "SPLIT", "SPOKE", "SPORT", "STAFF", "STAGE", "STAKE", "STAND", "START",
  "STATE", "STEAM", "STEEL", "STICK", "STILL", "STOCK", "STONE", "STORE",
  "STORM", "STORY", "STRIP", "STUCK", "STUDY", "STUFF", "STYLE", "SUGAR",
  "SUITE", "SUPER", "SWEET", "TABLE", "TAKEN", "TASTE", "TAXES", "TEACH",
  "TEETH", "TERRY", "TEXAS", "THANK", "THEFT", "THEIR", "THEME", "THERE",
  "THESE", "THICK", "THING", "THINK", "THIRD", "THOSE", "THREE", "THREW",
  "THROW", "TIGHT", "TIMES", "TIRED", "TITLE", "TODAY", "TOPIC", "TOTAL",
  "TOUCH", "TOUGH", "TOWER", "TRACK", "TRADE", "TRAIN", "TREAT", "TREND",
  "TRIAL", "TRIED", "TRUCK", "TRULY", "TRUST", "TRUTH", "TWICE", "UNDER",
  "UNDUE", "UNION", "UNITY", "UNTIL", "UPPER", "UPSET", "URBAN", "USAGE",
  "USUAL", "VALID", "VALUE", "VIDEO", "VIRUS", "VISIT", "VITAL", "VOICE",
  "WASTE", "WATCH", "WATER", "WHEEL", "WHERE", "WHICH", "WHILE", "WHITE",
  "WHOLE", "WHOSE", "WOMAN", "WOMEN", "WORLD", "WORRY", "WORSE", "WORST",
  "WORTH", "WOULD", "WOUND", "WRITE", "WRONG", "WROTE", "YIELD", "YOUNG",
  "YOUTH", "ZEBRA", "ZONES",
  // 6-letter words
  "ABSENT", "ACCEPT", "ACCESS", "ACROSS", "ACTION", "ACTIVE", "ACTUAL",
  "ADVICE", "ADVISE", "AGENCY", "AGENDA", "ALMOST", "ALWAYS", "AMOUNT",
  "ANIMAL", "ANNUAL", "ANSWER", "ANYONE", "ANYWAY", "APPEAL", "APPEAR",
  "AROUND", "ARRIVE", "ARTIST", "ASPECT", "ASSESS", "ASSIST", "ASSUME",
  "ATTACK", "ATTEND", "AUGUST", "AUTHOR", "AVENUE", "BACKED", "BARELY",
  "BATTLE", "BEAUTY", "BECAME", "BECOME", "BEFORE", "BEHALF", "BEHIND",
  "BELIEF", "BELONG", "BERLIN", "BETTER", "BEYOND", "BISHOP", "BORDER",
  "BOTTLE", "BOTTOM", "BOUGHT", "BRANCH", "BREATH", "BRIDGE", "BRIGHT",
  "BROKEN", "BUDGET", "BURDEN", "BUREAU", "BUTTON", "CAMERA", "CANCER",
  "CANNOT", "CARBON", "CAREER", "CASTLE", "CASUAL", "CAUGHT", "CENTER",
  "CENTRE", "CHANCE", "CHANGE", "CHARGE", "CHOICE", "CHOOSE", "CHOSEN",
  "CHURCH", "CIRCLE", "CLIENT", "CLOSED", "CLOSER", "COFFEE", "COLUMN",
  "COMBAT", "COMING", "COMMON", "COMPLY", "COPPER", "CORNER", "COSTLY",
  "COUNTY", "COUPLE", "COURSE", "COVERS", "CREATE", "CREDIT", "CRISIS",
  "CUSTOM", "DAMAGE", "DANGER", "DEALER", "DEBATE", "DECIDE", "DEFEAT",
  "DEFEND", "DEFINE", "DEGREE", "DEMAND", "DEPEND", "DEPUTY", "DESERT",
  "DESIGN", "DESIRE", "DETAIL", "DETECT", "DEVICE", "DIFFER", "DINNER",
  "DIRECT", "DOCTOR", "DOLLAR", "DOMAIN", "DOUBLE", "DRIVEN", "DRIVER",
  "DURING", "EASILY", "EATING", "EDITOR", "EFFECT", "EFFORT", "EIGHTY",
  "EITHER", "ELEVEN", "EMERGE", "EMPIRE", "EMPLOY", "ENDING", "ENERGY",
  "ENGAGE", "ENGINE", "ENOUGH", "ENSURE", "ENTIRE", "ENTITY", "EQUITY",
  "ESCAPE", "ESTATE", "ETHICS", "EXCEED", "EXCEPT", "EXCITE", "EXCUSE",
  "EXPAND", "EXPECT", "EXPERT", "EXPORT", "EXTEND", "EXTENT", "FABRIC",
  "FACING", "FACTOR", "FAILED", "FAIRLY", "FALLEN", "FAMILY", "FAMOUS",
  "FATHER", "FELLOW", "FEMALE", "FIGURE", "FILING", "FINGER", "FINISH",
  "FISCAL", "FLIGHT", "FLYING", "FOLLOW", "FORCED", "FOREST", "FORGET",
  "FORMAL", "FORMAT", "FORMER", "FOSTER", "FOUGHT", "FOURTH", "FRENCH",
  "FRIEND", "FUTURE", "GARDEN", "GATHER", "GENDER", "GERMAN", "GLOBAL",
  "GOLDEN", "GROUND", "GROWTH", "GUILTY", "HANDLE", "HAPPEN", "HARDLY",
  "HEALTH", "HEAVEN", "HEIGHT", "HIDDEN", "HOLDER", "HOLLOW", "HONEST",
  "HORROR", "HORSES", "HOSTILE", "HOUSING", "HUMBLE", "HUNGER", "HUNTED",
  "HUNTER", "IGNORE", "IMPACT", "IMPORT", "INCOME", "INDEED", "INJURY",
  "INSIDE", "INTEND", "INTENT", "INVEST", "ISLAND", "ITSELF", "JERSEY",
  "JUNIOR", "KILLED", "LABOUR", "LATEST", "LAUNCH", "LAWYER", "LEADER",
  "LEAGUE", "LEAVES", "LEGACY", "LENGTH", "LESSON", "LETTER", "LIGHTS",
  "LIKELY", "LISTEN", "LITTLE", "LIVING", "LOCATE", "LONDON", "LONELY",
  "LOSING", "LOVELY", "LUXURY", "MAINLY", "MAKING", "MANAGE", "MANNER",
  "MANUAL", "MARGIN", "MARINE", "MARKED", "MARKET", "MARTIN", "MASTER",
  "MATTER", "MATURE", "MEDIUM", "MEMBER", "MEMORY", "MENTAL", "MERELY",
  "MERGER", "METHOD", "MIDDLE", "MILLER", "MINING", "MINUTE", "MIRROR",
  "MOBILE", "MODERN", "MODEST", "MODULE", "MOMENT", "MORRIS", "MOSTLY",
  "MOTHER", "MOTION", "MOVING", "MURDER", "MUSEUM", "MUTUAL", "MYSELF",
  "NARROW", "NATION", "NATIVE", "NATURE", "NEARLY", "NIGHTS", "NOBODY",
  "NORMAL", "NOTICE", "NOTION", "NUMBER", "OBJECT", "OBTAIN", "OFFICE",
  "OFFSET", "ONLINE", "OPTION", "ORANGE", "ORIGIN", "OUTPUT", "OWNERS",
  "OXFORD", "PACKED", "PALACE", "PARENT", "PARKER", "PARTLY", "PATENT",
  "PEOPLE", "PERIOD", "PERMIT", "PERSON", "PHRASE", "PICKED", "PLANET",
  "PLAYER", "PLEASE", "PLENTY", "POCKET", "POLICE", "POLICY", "PREFER",
  "PRETTY", "PRINCE", "PRISON", "PROFIT", "PROPER", "PROVE", "PUBLIC",
  "PURSUE", "RAISED", "RANDOM", "RARELY", "RATHER", "READER", "REALLY",
  "REASON", "RECALL", "RECENT", "RECORD", "REDUCE", "REFORM", "REGARD",
  "REGIME", "REGION", "RELATE", "RELief", "REMOTE", "REMOVE", "REPAIR",
  "REPEAT", "REPLAY", "REPORT", "RESCUE", "RESORT", "RESULT", "RETAIL",
  "RETAIN", "RETURN", "REVEAL", "REVIEW", "RHYTHM", "RIDING", "RISING",
  "ROBUST", "ROCKET", "ROLLER", "ROMMAN", "SAFETY", "SALARY", "SAMPLE",
  "SAVING", "SCHEME", "SCHOOL", "SCREAM", "SCREEN", "SCRIPT", "SEARCH",
  "SEASON", "SECOND", "SECRET", "SECTOR", "SECURE", "SEEING", "SELECT",
  "SELLER", "SENIOR", "SERIES", "SERVER", "SETTLE", "SEVERE", "SEXUAL",
  "SHOULD", "SIGNAL", "SIGNED", "SILENT", "SILVER", "SIMPLE", "SIMPLY",
  "SINGLE", "SISTER", "SLIGHT", "SMOOTH", "SOCIAL", "SOCIETY", "SOFTLY",
  "SOLELY", "SOUGHT", "SOURCE", "SOVIET", "SPEECH", "SPIRIT", "SPOKEN",
  "SPREAD", "SPRING", "SQUARE", "STABLE", "STATUS", "STEADY", "STOLEN",
  "STRAIN", "STREAM", "STREET", "STRESS", "STRICT", "STRIKE", "STRING",
  "STRONG", "STRUCK", "STUDIO", "SUBMIT", "SUDDEN", "SUFFER", "SUMMER",
  "SUMMIT", "SUPPLY", "SURELY", "SURVEY", "SWITCH", "SYMBOL", "SYSTEM",
  "TAKING", "TALENT", "TARGET", "TAUGHT", "TENANT", "TENDER", "TENNIS",
  "THANKS", "THEORY", "THIRTY", "THOUGH", "THREAT", "THROWN", "TICKET",
  "TIMING", "TISSUE", "TONGUE", "TOPICS", "TOUCH", "TOWARD", "TRAVEL",
  "TREATY", "TRYING", "TWELVE", "TWENTY", "UNABLE", "UNIQUE", "UNITED",
  "UNLESS", "UNLIKE", "UPDATE", "USEFUL", "VALLEY", "VARIED", "VENDOR",
  "VERSUS", "VICTIM", "VISION", "VISUAL", "VOLUME", "WALKER", "WANTED",
  "WARNING", "WEALTH", "WEEKLY", "WEIGHT", "WHOLLY", "WINDOW", "WINNER",
  "WINTER", "WITHIN", "WONDER", "WORKER", "WRIGHT", "WRITER", "YELLOW"
]);

// Get today's word based on date
function getTodaysWord() {
  const now = new Date();
  const start = new Date(2024, 0, 1); // Jan 1, 2024
  const diff = Math.floor((now - start) / (1000 * 60 * 60 * 24));
  return WORDS[diff % WORDS.length];
}

// Check if a guess is valid (exists in word list or is a common word)
function isValidGuess(guess) {
  const upperGuess = guess.toUpperCase();
  return VALID_GUESSES.has(upperGuess);
}

// Get letter feedback (green/yellow/gray) for a guess
function getLetterFeedback(guess, target) {
  const result = [];
  const targetLetters = target.split('');
  const guessLetters = guess.toUpperCase().split('');
  
  // First pass: mark exact matches (green)
  for (let i = 0; i < guessLetters.length; i++) {
    if (guessLetters[i] === targetLetters[i]) {
      result[i] = 'correct';
      targetLetters[i] = null; // Mark as used
    }
  }
  
  // Second pass: mark present but wrong position (yellow)
  for (let i = 0; i < guessLetters.length; i++) {
    if (result[i]) continue; // Already marked as correct
    
    const index = targetLetters.indexOf(guessLetters[i]);
    if (index !== -1) {
      result[i] = 'present';
      targetLetters[index] = null; // Mark as used
    } else {
      result[i] = 'absent';
    }
  }
  
  return result;
}

module.exports = {
  WORDS,
  VALID_GUESSES,
  getTodaysWord,
  isValidGuess,
  getLetterFeedback
};
