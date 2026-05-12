/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        'rvr-bg': '#020816',
        'rvr-card': '#050c1c',
        'rvr-cyan': '#00c8ff',
        'rvr-gold': '#ffc800',
        'rvr-silver': '#8cbef0',
        'rvr-bronze': '#be6928',
      },
      animation: {
        'flip': 'flip 0.5s ease-in-out',
        'shake': 'shake 0.5s ease-in-out',
        'pop': 'pop 0.1s ease-in-out',
      },
      keyframes: {
        flip: {
          '0%': { transform: 'rotateX(0deg)' },
          '50%': { transform: 'rotateX(90deg)' },
          '100%': { transform: 'rotateX(0deg)' },
        },
        shake: {
          '0%, 100%': { transform: 'translateX(0)' },
          '25%': { transform: 'translateX(-5px)' },
          '75%': { transform: 'translateX(5px)' },
        },
        pop: {
          '0%, 100%': { transform: 'scale(1)' },
          '50%': { transform: 'scale(1.1)' },
        },
      },
    },
  },
  plugins: [],
}
