# RVR-Wordle 🏎️

A real-time multiplayer Wordle game integrated with Discord for RVR Underground.

## Features

- **4 Game Modes**: Classic, Race, Battle (PvP sabotage), Time Attack
- **Real-time Multiplayer**: Play with others in real-time using Socket.io
- **Discord Integration**: Auto-login via Discord OAuth, !wordle commands
- **RV-themed Words**: Custom word list with RV/RVGL themed words
- **Stats & Leaderboards**: Track progress and compete for top spots
- **Mobile Friendly**: Responsive design works on all devices

## Tech Stack

- **Frontend**: React + Vite + TailwindCSS
- **Backend**: Node.js + Express + Socket.io
- **Database**: MongoDB (shared with rvr-bot)
- **Auth**: Discord OAuth2 + JWT
- **Hosting**: Railway (backend) + Netlify (frontend)

## Quick Start

### Prerequisites
- Node.js 18+
- MongoDB database
- Discord Application credentials

### Setup

1. **Clone and install dependencies**
```bash
# Backend
cd rvr-wordle/server
npm install
cp .env.example .env

# Frontend  
cd ../web
npm install
cp .env.example .env
```

2. **Configure environment variables**

Backend (.env):
```env
DISCORD_CLIENT_ID=your_discord_client_id
DISCORD_CLIENT_SECRET=your_discord_client_secret
DISCORD_CALLBACK_URL=http://localhost:3001/auth/discord/callback
JWT_SECRET=your_random_jwt_secret_here
MONGO_URL=mongodb+srv://username:password@cluster.mongodb.net/rvr_underground
PORT=3001
FRONTEND_URL=http://localhost:5173
```

Frontend (.env):
```env
VITE_API_URL=http://localhost:3001
VITE_DISCORD_CLIENT_ID=your_discord_client_id
VITE_DISCORD_REDIRECT_URI=http://localhost:5173/auth/callback
```

3. **Start development servers**
```bash
# Backend (terminal 1)
cd rvr-wordle/server
npm run dev

# Frontend (terminal 2)
cd rvr-wordle/web
npm run dev
```

4. **Test Discord integration**
- Add the bot commands to your rvr-bot
- Use `!wordle` to launch the game
- Use `!wordlestats` to view stats
- Use `!wordleboard` to view leaderboards

## Game Modes

### 🎯 Classic
Traditional Wordle - 6 guesses to solve today's word

### 🏁 Race  
Real-time competition - solve faster than others

### ⚔️ Battle
PvP sabotage - earn oil cans to block letters and sabotage opponents

### ⏱️ Time Attack
Speed run - solve as fast as possible

## Discord Commands

- `!wordle` - Launch the web app with auto-login
- `!wordlestats [@user]` - Show Wordle statistics
- `!wordleboard [mode]` - Show leaderboard (classic/race/timeattack)

## Deployment

### Railway (Backend)
1. Connect your GitHub repo to Railway
2. Set environment variables in Railway dashboard
3. Deploy will auto-trigger on push to main

### Netlify (Frontend)
1. Connect your GitHub repo to Netlify  
2. Set build command: `npm run build`
3. Set publish directory: `dist`
4. Configure environment variables

## API Endpoints

- `GET /api/daily-word` - Get today's word info
- `POST /api/verify-token` - Verify JWT token
- `POST /api/generate-token` - Generate token for bot integration
- `GET /auth/discord` - Discord OAuth login
- `GET /auth/discord/callback` - Discord OAuth callback

## Socket.io Events

### Client → Server
- `authenticate` - Send JWT token
- `join_mode` - Join a game mode
- `submit_guess` - Submit a word guess
- `use_oil` - Use oil can (battle mode)
- `time_attack_submit` - Submit time (time attack)
- `get_leaderboard` - Get current leaderboard

### Server → Client
- `authenticated` - Authentication successful
- `mode_joined` - Successfully joined game mode
- `guess_result` - Result of word guess
- `game_won` - Game completed successfully
- `game_lost` - Game failed
- `leaderboard` - Leaderboard data
- `player_joined` - Player joined race mode
- `player_solved` - Player solved in race mode

## Contributing

1. Fork the repo
2. Create feature branch
3. Make changes
4. Test thoroughly
5. Submit pull request

## License

MIT License - see LICENSE file for details
