/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      fontFamily: { sans: ['Plus Jakarta Sans', 'ui-sans-serif', 'system-ui'] },
      colors: { brand: { 50: '#eef4ff', 100: '#dfeaff', 500: '#4f6ef7', 600: '#2563eb', 700: '#1d4ed8' } },
      boxShadow: { soft: '0 8px 30px rgba(37, 99, 235, 0.06)', glow: '0 8px 24px rgba(37, 99, 235, 0.22)' },
      borderRadius: { card: '16px' },
    },
  },
  plugins: [],
}
