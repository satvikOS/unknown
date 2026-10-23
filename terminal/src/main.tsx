import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { RuntimeProvider } from './state/react';
import { installMarketDataWiring } from './state/marketdataWiring';
import { createRuntime } from './state/runtime';
import { applyUpDown } from './ui/marketPrefs';
import { applyTheme } from './ui/theme';
import './ui/styles.css';
import './ui/shell.css';

applyTheme();
applyUpDown();
installMarketDataWiring();

const runtime = createRuntime();
const root = document.getElementById('root');
if (!root) throw new Error('#root missing from index.html');

createRoot(root).render(
  <StrictMode>
    <RuntimeProvider runtime={runtime}>
      <App />
    </RuntimeProvider>
  </StrictMode>,
);
