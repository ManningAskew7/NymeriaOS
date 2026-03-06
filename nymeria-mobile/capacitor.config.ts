import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.nymeria.mobile',
  appName: 'Nymeria',
  webDir: 'build',
  server: {
    // During development, point to Vite dev server on LAN
    // url: 'http://192.168.1.100:5173',
    // cleartext: true,
    androidScheme: 'https',
  },
  plugins: {
    SplashScreen: {
      launchAutoHide: true,
      launchShowDuration: 2000,
      backgroundColor: '#121417',
      showSpinner: false,
    },
    Keyboard: {
      resize: 'none', // We handle layout manually for chat input
      resizeOnFullScreen: true,
    },
    StatusBar: {
      style: 'DARK',
      backgroundColor: '#121417',
    },
  },
};

export default config;
