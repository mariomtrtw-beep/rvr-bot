const express = require('express');
const http = require('http');
const socketIo = require('socket.io');
const cors = require('cors');
const session = require('express-session');
const passport = require('passport');
const { MongoClient } = require('mongodb');
const jwt = require('jsonwebtoken');
const { v4: uuidv4 } = require('uuid');
require('dotenv').config();

const words = require('./words');
const authRoutes = require('./auth');
const gameLogic = require('./game-logic');

const app = express();
const server = http.createServer(app);
const io = socketIo(server, {
  cors: {
    origin: process.env.FRONTEND_URL || "http://localhost:5173",
    methods: ["GET", "POST"],
    credentials: true
  }
});

// MongoDB connection
let db;
let wordleDailyCollection;
let wordleUsersCollection;

async function connectDB() {
  const client = new MongoClient(process.env.MONGO_URL);
  await client.connect();
  db = client.db('rvr_underground');
  wordleDailyCollection = db.collection('wordle_daily');
  wordleUsersCollection = db.collection('wordle_users');
  console.log('✅ Connected to MongoDB');
}

// Middleware
app.use(cors({
  origin: process.env.FRONTEND_URL || "http://localhost:5173",
  credentials: true
}));
app.use(express.json());
app.use(session({
  secret: process.env.JWT_SECRET || 'secret',
  resave: false,
  saveUninitialized: false,
  cookie: { secure: false } // Set to true in production with HTTPS
}));
app.use(passport.initialize());
app.use(passport.session());

// Routes
app.use('/auth', authRoutes);

// Health check endpoint
app.get('/health', (req, res) => {
  res.status(200).send('OK');
});

// API endpoint to get daily word info
app.get('/api/daily-word', async (req, res) => {
  try {
    const today = new Date().toISOString().split('T')[0];
    const word = words.getTodaysWord();
    
    // Get or create daily record
    let daily = await wordleDailyCollection.findOne({ date: today });
    if (!daily) {
      daily = {
        date: today,
        word: word,
        modes: {
          classic: { solves: [] },
          race: { active: true, players: [] },
          battle: { active: true, players: [], oilCans: {} },
          timeattack: { entries: [] }
        }
      };
      await wordleDailyCollection.insertOne(daily);
    }
    
    res.json({
      date: today,
      wordLength: word.length,
      // Don't send the actual word to client!
    });
  } catch (error) {
    console.error('Error getting daily word:', error);
    res.status(500).json({ error: 'Server error' });
  }
});

// API endpoint to verify token
app.post('/api/verify-token', async (req, res) => {
  try {
    const { token } = req.body;
    const decoded = jwt.verify(token, process.env.JWT_SECRET);
    
    // Get or create user
    let user = await wordleUsersCollection.findOne({ uid: decoded.uid });
    if (!user) {
      user = {
        uid: decoded.uid,
        discordName: decoded.username,
        stats: {
          classic: { played: 0, wins: 0, streak: 0, maxStreak: 0 },
          race: { played: 0, wins: 0, firstBloods: 0 },
          battle: { played: 0, wins: 0, oilUsed: 0, oilReceived: 0 },
          timeattack: { played: 0, topTimes: [] }
        },
        achievements: [],
        banned: false
      };
      await wordleUsersCollection.insertOne(user);
    }
    
    res.json({
      user: {
        uid: user.uid,
        discordName: user.discordName,
        stats: user.stats,
        achievements: user.achievements
      }
    });
  } catch (error) {
    res.status(401).json({ error: 'Invalid token' });
  }
});

// Socket.io connection handling
const activeGames = new Map(); // userId -> game state
const racePlayers = new Set(); // Set of userIds in race mode
const battlePlayers = new Map(); // userId -> { oilCans, targetBlocked }

io.on('connection', (socket) => {
  console.log('User connected:', socket.id);
  
  let userId = null;
  let currentMode = null;
  let gameState = null;
  
  // Authenticate user
  socket.on('authenticate', async (token) => {
    try {
      const decoded = jwt.verify(token, process.env.JWT_SECRET);
      userId = decoded.uid;
      socket.userId = userId;
      socket.username = decoded.username;
      
      // Load user's game state for today
      const today = new Date().toISOString().split('T')[0];
      const daily = await wordleDailyCollection.findOne({ date: today });
      
      // Check if user already played today
      const userGame = activeGames.get(userId);
      if (userGame && userGame.date === today) {
        gameState = userGame;
      } else {
        gameState = {
          date: today,
          guesses: [],
          solved: false,
          startTime: Date.now()
        };
        activeGames.set(userId, gameState);
      }
      
      socket.emit('authenticated', {
        userId,
        username: decoded.username,
        gameState: {
          guesses: gameState.guesses,
          solved: gameState.solved
        }
      });
      
    } catch (error) {
      socket.emit('auth_error', { message: 'Invalid token' });
    }
  });
  
  // Join a game mode
  socket.on('join_mode', async (mode) => {
    if (!userId) return;
    
    currentMode = mode;
    socket.join(mode);
    
    const today = new Date().toISOString().split('T')[0];
    const word = words.getTodaysWord();
    
    if (mode === 'race') {
      racePlayers.add(userId);
      
      // Notify others
      socket.to('race').emit('player_joined', {
        userId,
        username: socket.username,
        guessCount: gameState.guesses.length,
        solved: gameState.solved
      });
      
      // Send current race state
      const raceState = [];
      for (const pid of racePlayers) {
        const playerGame = activeGames.get(pid);
        if (playerGame && pid !== userId) {
          // Get username from somewhere - simplified
          raceState.push({
            userId: pid,
            guessCount: playerGame.guesses.length,
            solved: playerGame.solved
          });
        }
      }
      socket.emit('race_state', raceState);
      
    } else if (mode === 'battle') {
      battlePlayers.set(userId, { oilCans: 0, blockedLetters: [] });
      
      // Check if user already has oil cans from previous solves
      const daily = await wordleDailyCollection.findOne({ date: today });
      if (daily && daily.modes.battle.oilCans && daily.modes.battle.oilCans[userId]) {
        battlePlayers.get(userId).oilCans = daily.modes.battle.oilCans[userId];
      }
    }
    
    socket.emit('mode_joined', { mode, wordLength: word.length });
  });
  
  // Submit a guess
  socket.on('submit_guess', async (guess) => {
    if (!userId || !gameState || gameState.solved) return;
    
    const upperGuess = guess.toUpperCase();
    const today = new Date().toISOString().split('T')[0];
    const targetWord = words.getTodaysWord();
    
    // Validate guess
    if (upperGuess.length !== targetWord.length) {
      socket.emit('invalid_guess', { message: `Word must be ${targetWord.length} letters` });
      return;
    }
    
    if (!words.isValidGuess(upperGuess)) {
      socket.emit('invalid_guess', { message: 'Not in word list' });
      return;
    }
    
    // Check for blocked letters (battle mode)
    if (currentMode === 'battle') {
      const playerBattle = battlePlayers.get(userId);
      if (playerBattle && playerBattle.blockedLetters.length > 0) {
        const hasBlocked = upperGuess.split('').some((letter, i) => 
          playerBattle.blockedLetters.includes(letter)
        );
        if (hasBlocked) {
          socket.emit('invalid_guess', { message: 'Contains blocked letters!' });
          return;
        }
        // Clear blocked letters after this guess
        playerBattle.blockedLetters = [];
      }
    }
    
    // Get feedback
    const feedback = words.getLetterFeedback(upperGuess, targetWord);
    
    // Add to game state
    gameState.guesses.push({
      word: upperGuess,
      feedback,
      timestamp: Date.now()
    });
    
    // Check if solved
    const isSolved = feedback.every(f => f === 'correct');
    if (isSolved) {
      gameState.solved = true;
      const solveTime = Date.now() - gameState.startTime;
      
      // Update stats
      await updateUserStats(userId, currentMode, {
        won: true,
        guesses: gameState.guesses.length,
        time: solveTime
      });
      
      // Save to daily record
      await saveSolveToDaily(userId, socket.username, currentMode, {
        guesses: gameState.guesses.length,
        time: solveTime
      });
      
      // Battle mode: give oil cans
      if (currentMode === 'battle') {
        const oilEarned = Math.max(1, 7 - gameState.guesses.length); // Fewer guesses = more oil
        const playerBattle = battlePlayers.get(userId);
        if (playerBattle) {
          playerBattle.oilCans += oilEarned;
          await wordleDailyCollection.updateOne(
            { date: today },
            { $set: { [`modes.battle.oilCans.${userId}`]: playerBattle.oilCans } }
          );
        }
        socket.emit('oil_earned', { amount: oilEarned, total: playerBattle.oilCans });
      }
      
      socket.emit('game_won', {
        guesses: gameState.guesses.length,
        time: solveTime,
        word: targetWord
      });
      
      // Notify race mode players
      if (currentMode === 'race') {
        socket.to('race').emit('player_solved', {
          userId,
          username: socket.username,
          guesses: gameState.guesses.length,
          time: solveTime
        });
      }
      
    } else if (gameState.guesses.length >= 6) {
      // Game over
      await updateUserStats(userId, currentMode, { won: false, guesses: 6 });
      socket.emit('game_lost', { word: targetWord });
    } else {
      socket.emit('guess_result', {
        guess: upperGuess,
        feedback,
        remaining: 6 - gameState.guesses.length
      });
      
      // Update race mode
      if (currentMode === 'race') {
        socket.to('race').emit('player_progress', {
          userId,
          username: socket.username,
          guessCount: gameState.guesses.length
        });
      }
    }
  });
  
  // Battle mode: use oil can
  socket.on('use_oil', async (targetUserId, action) => {
    if (!userId || currentMode !== 'battle') return;
    
    const playerBattle = battlePlayers.get(userId);
    if (!playerBattle || playerBattle.oilCans < action.cost) {
      socket.emit('oil_error', { message: 'Not enough oil cans!' });
      return;
    }
    
    // Deduct oil
    playerBattle.oilCans -= action.cost;
    const today = new Date().toISOString().split('T')[0];
    await wordleDailyCollection.updateOne(
      { date: today },
      { $set: { [`modes.battle.oilCans.${userId}`]: playerBattle.oilCans } }
    );
    
    // Apply effect
    const targetSocket = Array.from(io.sockets.sockets.values())
      .find(s => s.userId === targetUserId);
    
    if (targetSocket) {
      if (action.type === 'block_letter') {
        // Block a random letter
        const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
        const blocked = letters[Math.floor(Math.random() * letters.length)];
        const targetBattle = battlePlayers.get(targetUserId);
        if (targetBattle) {
          targetBattle.blockedLetters.push(blocked);
        }
        targetSocket.emit('letter_blocked', { letter: blocked });
        socket.emit('oil_used', { type: 'block_letter', target: targetUserId, letter: blocked });
        
      } else if (action.type === 'skip_turn') {
        targetSocket.emit('turn_skipped');
        socket.emit('oil_used', { type: 'skip_turn', target: targetUserId });
      }
    }
  });
  
  // Time attack: submit time
  socket.on('time_attack_submit', async (timeMs, guesses) => {
    if (!userId) return;
    
    const today = new Date().toISOString().split('T')[0];
    const entry = {
      userId,
      username: socket.username,
      timeMs,
      guesses,
      timestamp: Date.now()
    };
    
    await wordleDailyCollection.updateOne(
      { date: today },
      { $push: { 'modes.timeattack.entries': entry } }
    );
    
    // Update personal best
    const user = await wordleUsersCollection.findOne({ uid: userId });
    if (user) {
      const topTimes = user.stats.timeattack.topTimes || [];
      topTimes.push({ word: words.getTodaysWord(), timeMs, date: today });
      topTimes.sort((a, b) => a.timeMs - b.timeMs);
      if (topTimes.length > 10) topTimes.pop();
      
      await wordleUsersCollection.updateOne(
        { uid: userId },
        { 
          $set: { 'stats.timeattack.topTimes': topTimes },
          $inc: { 'stats.timeattack.played': 1 }
        }
      );
    }
    
    socket.emit('time_attack_recorded', entry);
  });
  
  // Get leaderboard
  socket.on('get_leaderboard', async (mode) => {
    const today = new Date().toISOString().split('T')[0];
    const daily = await wordleDailyCollection.findOne({ date: today });
    
    if (!daily || !daily.modes[mode]) {
      socket.emit('leaderboard', []);
      return;
    }
    
    let leaderboard = [];
    
    if (mode === 'classic') {
      leaderboard = daily.modes.classic.solves
        .sort((a, b) => a.guesses - b.guesses || a.time - b.time)
        .slice(0, 10);
    } else if (mode === 'race') {
      leaderboard = daily.modes.race.players
        .filter(p => p.solved)
        .sort((a, b) => a.solveTime - b.solveTime)
        .slice(0, 10);
    } else if (mode === 'timeattack') {
      leaderboard = daily.modes.timeattack.entries
        .sort((a, b) => a.timeMs - b.timeMs)
        .slice(0, 10);
    }
    
    socket.emit('leaderboard', leaderboard);
  });
  
  // Disconnect
  socket.on('disconnect', () => {
    console.log('User disconnected:', socket.id);
    if (userId) {
      racePlayers.delete(userId);
      battlePlayers.delete(userId);
      socket.to(currentMode).emit('player_left', { userId });
    }
  });
});

// Helper functions
async function updateUserStats(userId, mode, result) {
  const update = {};
  
  if (mode === 'classic') {
    update.$inc = {
      'stats.classic.played': 1,
      'stats.classic.wins': result.won ? 1 : 0
    };
    if (result.won) {
      update.$inc['stats.classic.streak'] = 1;
    } else {
      update.$set = { 'stats.classic.streak': 0 };
    }
  } else if (mode === 'race') {
    update.$inc = {
      'stats.race.played': 1,
      'stats.race.wins': result.won ? 1 : 0
    };
  } else if (mode === 'battle') {
    update.$inc = {
      'stats.battle.played': 1,
      'stats.battle.wins': result.won ? 1 : 0
    };
  }
  
  await wordleUsersCollection.updateOne({ uid: userId }, update, { upsert: true });
}

async function saveSolveToDaily(userId, username, mode, result) {
  const today = new Date().toISOString().split('T')[0];
  const entry = {
    userId,
    username,
    guesses: result.guesses,
    time: result.time,
    timestamp: Date.now()
  };
  
  if (mode === 'classic') {
    await wordleDailyCollection.updateOne(
      { date: today },
      { $push: { 'modes.classic.solves': entry } }
    );
  } else if (mode === 'race') {
    await wordleDailyCollection.updateOne(
      { date: today, 'modes.race.players.userId': { $ne: userId } },
      { $push: { 'modes.race.players': { ...entry, solved: true, solveTime: result.time } } }
    );
  }
}

// Start server
const PORT = process.env.PORT || 3001;

connectDB().then(() => {
  server.listen(PORT, '0.0.0.0', () => {
    console.log(`🚀 RVR-Wordle server running on port ${PORT}`);
  });
});

module.exports = { io, db };
