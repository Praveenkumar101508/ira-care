import Constants from 'expo-constants';
import { registerRootComponent } from 'expo';
import { LogBox } from 'react-native';
import App from './src/App';

// The simulator journey's test build only (its workflow sets extra.quietDevWarnings; app.json does
// not): a Debug build draws its "Open debugger to view warnings" bar over the tab bar, and taps meant
// for Home and Settings landed on it (run 46071161313). The warnings still reach the console log.
if ((Constants.expoConfig?.extra as { quietDevWarnings?: boolean } | undefined)?.quietDevWarnings) {
  LogBox.ignoreAllLogs(true);
}

registerRootComponent(App);
