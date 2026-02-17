import { spawn } from 'child_process';

console.log('Starting Python Seeding...');
const seed = spawn('python', ['seed.py'], { stdio: 'inherit' });

seed.on('close', (code) => {
  if (code === 0) {
    console.log('Seeding complete. Starting Python App...');
    const app = spawn('python', ['app.py'], { stdio: 'inherit' });
    
    app.on('close', (code) => {
        console.log(`Python App exited with code ${code}`);
        process.exit(code || 0);
    });
  } else {
    console.error(`Seeding failed with code ${code}`);
    process.exit(code || 1);
  }
});
