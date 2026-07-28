#!/usr/bin/env node
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const root = path.resolve(__dirname, '..');
const enterpriseDir = path.join(root, 'aviator_enterprise');
const pythonCandidates = [
  path.join(enterpriseDir, '.venv-enterprise', 'bin', 'python'),
  path.join(enterpriseDir, '.venv-enterprise', 'Scripts', 'python.exe'),
  'python3',
  'python',
];

function findPython() {
  for (const candidate of pythonCandidates) {
    if (!candidate.includes(path.sep)) {
      return candidate;
    }
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return 'python3';
}

const pythonBin = findPython();
const args = ['-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000'];

console.log(`[enterprise] starting ${pythonBin} ${args.join(' ')}`);
const child = spawn(pythonBin, args, {
  cwd: enterpriseDir,
  env: process.env,
  stdio: 'inherit',
});

function stop(code = 0) {
  if (!child.killed) {
    child.kill('SIGTERM');
  }
  setTimeout(() => process.exit(code), 300);
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));

child.on('exit', (code, signal) => {
  if (signal) {
    console.error(`[enterprise] exited with signal ${signal}`);
    process.exit(1);
  }
  process.exit(code ?? 0);
});
