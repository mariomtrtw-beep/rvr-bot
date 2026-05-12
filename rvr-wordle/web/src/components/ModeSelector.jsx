import React from 'react'

export default function ModeSelector({ user, onModeSelect }) {
  const modes = [
    {
      id: 'classic',
      name: 'Classic',
      icon: '🎯',
      description: 'Traditional Wordle - 6 guesses to solve today\'s word',
      color: 'from-green-500 to-green-600'
    },
    {
      id: 'race',
      name: 'Race',
      icon: '🏁',
      description: 'Real-time competition - solve faster than others',
      color: 'from-blue-500 to-blue-600'
    },
    {
      id: 'battle',
      name: 'Battle',
      icon: '⚔️',
      description: 'PvP sabotage - earn oil cans to block letters',
      color: 'from-red-500 to-red-600'
    },
    {
      id: 'timeattack',
      name: 'Time Attack',
      icon: '⏱️',
      description: 'Speed run - solve as fast as possible',
      color: 'from-purple-500 to-purple-600'
    }
  ]

  return (
    <div className="min-h-screen bg-rvr-bg flex items-center justify-center p-4">
      <div className="max-w-4xl w-full">
        {/* Header */}
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold text-rvr-cyan mb-4">
            🏎️ RVR-Wordle
          </h1>
          <div className="flex items-center justify-center space-x-4 mb-8">
            <img 
              src={`https://cdn.discordapp.com/avatars/${user.uid}/${user.avatar}.png`} 
              alt={user.username}
              className="w-12 h-12 rounded-full"
            />
            <div>
              <p className="text-xl font-semibold">{user.username}</p>
              <p className="text-gray-400">Choose your game mode</p>
            </div>
          </div>
        </div>

        {/* Mode Cards */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          {modes.map(mode => (
            <div
              key={mode.id}
              onClick={() => onModeSelect(mode.id)}
              className={`mode-card bg-gradient-to-r ${mode.color} p-8 cursor-pointer transform transition-all duration-200 hover:scale-105`}
            >
              <div className="text-center">
                <div className="text-6xl mb-4">{mode.icon}</div>
                <h2 className="text-2xl font-bold mb-3">{mode.name}</h2>
                <p className="text-gray-200">{mode.description}</p>
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="text-center mt-12 text-gray-500">
          <p>RV-themed multiplayer Wordle for RVR Underground</p>
          <p className="text-sm mt-2">Built with React + Socket.io</p>
        </div>
      </div>
    </div>
  )
}
