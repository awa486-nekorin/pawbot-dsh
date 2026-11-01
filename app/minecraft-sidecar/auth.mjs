#!/usr/bin/env node
import { createAuthflow } from './src/auth.js';

// Never imported by index.mjs. Human-only, explicit setup; no game connection.
if (!process.stdin.isTTY || !process.stdout.isTTY || process.argv.length !== 3 || !/^[A-Za-z0-9_]{1,16}$/.test(process.argv[2])) {
  process.stderr.write('Run local auth setup in an interactive terminal with the configured account alias.\n');
  process.exitCode = 2;
} else {
  const print = process.stdout.write.bind(process.stdout);
  console.log = console.warn = console.error = console.info = console.debug = () => {};
  delete process.env.DEBUG;
  const timeout = setTimeout(() => { process.stderr.write('Local auth setup timed out.\n'); process.exit(2); }, 180000);
  try {
    const flow = await createAuthflow(process.argv[2], process.env.PAWBOT_MC_AUTH_DIR, { interactive: true, display: (code) => {
      if (/^[A-Za-z0-9-]{4,32}$/.test(code.user_code || '')) print(`Open https://www.microsoft.com/link and enter: ${code.user_code}\n`);
    } });
    const result = await flow.getMinecraftJavaToken({ fetchProfile: true });
    if (!result.profile?.id) throw new Error();
    print('Local authentication cache ready. No game connection was opened.\n');
  } catch { process.stderr.write('Local auth setup failed.\n'); process.exitCode = 2; }
  finally { clearTimeout(timeout); }
}
