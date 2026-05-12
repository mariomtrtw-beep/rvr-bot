const express = require('express');
const passport = require('passport');
const DiscordStrategy = require('passport-discord').Strategy;
const jwt = require('jsonwebtoken');

const router = express.Router();

// Discord OAuth configuration
passport.use(new DiscordStrategy({
  clientID: process.env.DISCORD_CLIENT_ID,
  clientSecret: process.env.DISCORD_CLIENT_SECRET,
  callbackURL: process.env.DISCORD_CALLBACK_URL,
  scope: ['identify']
}, (accessToken, refreshToken, profile, done) => {
  // Create JWT token with user info
  const token = jwt.sign({
    uid: profile.id,
    username: profile.username,
    avatar: profile.avatar
  }, process.env.JWT_SECRET, { expiresIn: '1h' });
  
  done(null, { token, user: profile });
}));

passport.serializeUser((user, done) => done(null, user));
passport.deserializeUser((user, done) => done(null, user));

// Discord OAuth routes
router.get('/discord', passport.authenticate('discord'));

router.get('/discord/callback',
  passport.authenticate('discord', { failureRedirect: '/auth/failed' }),
  (req, res) => {
    // Redirect to frontend with token
    const { token } = req.user;
    const frontendUrl = process.env.FRONTEND_URL || 'http://localhost:5173';
    res.redirect(`${frontendUrl}/auth/callback?token=${token}`);
  }
);

// JWT token generation endpoint (for bot integration)
router.post('/generate-token', (req, res) => {
  const { uid, username } = req.body;
  
  if (!uid || !username) {
    return res.status(400).json({ error: 'Missing uid or username' });
  }
  
  const token = jwt.sign({
    uid,
    username,
    source: 'bot'
  }, process.env.JWT_SECRET, { expiresIn: '1h' });
  
  res.json({ token });
});

// Failed auth
router.get('/failed', (req, res) => {
  res.status(401).json({ error: 'Authentication failed' });
});

module.exports = router;
