/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#ECFDF5',
          100: '#D1FAE5',
          200: '#A7F3D0',
          300: '#6EE7B7',
          400: '#34D399',
          500: '#10B981',
          600: '#059669',
          700: '#047857',
          800: '#065F46',
          900: '#064E3B',
        },
        darkbg: {
          base: '#041712',
          surface: '#07261d',
          card: '#0a3327',
          elevated: '#0d3d2f',
          border: '#114a39',
          hover: '#0e4233',
        },
      },
    },
  },
  plugins: [],
};
