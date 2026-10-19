import { buildApp } from './app.js';

async function main(): Promise<void> {
  const { app, context, seed, close } = await buildApp();

  const shutdown = async (signal: string): Promise<void> => {
    app.log.info({ signal }, 'shutting down');
    await close();
    process.exit(0);
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));

  await app.listen({ port: context.config.port, host: context.config.host });
  app.log.info(
    {
      seed: context.config.faultSeed,
      faultScenarios: context.engine.scenarios.length,
      scenarioSource: context.engine.load.source,
      db: context.config.dbPath,
      seeded: seed,
    },
    'mock-api ready',
  );
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
