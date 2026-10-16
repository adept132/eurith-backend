"""Read-only HTTP/client deadline seam against a retained local benchmark owner.

Requires P108_TEST_DATABASE_URL_FILE or TEST_DATABASE_URL; never prints credentials.
Run from B with --mobile-root <M> --owner-id <retained synthetic owner>.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from urllib.parse import urlparse

B = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(B/'scripts'))
from run_p108_tests import configure


async def main(args):
    import uvicorn
    from sqlalchemy import select, func
    from app.database import SessionLocal, engine
    from api.main import app
    from api.deps import get_current_firebase_claims
    from api.services.models import AppUser, Exercise, TrainingBlock, WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
    os.environ['P108_ANALYTICS_ENABLED'] = 'true'
    async with SessionLocal() as db:
        owner = await db.get(AppUser, args.owner_id)
        assert owner and owner.firebase_uid.startswith('p108-benchmark-')
        exercises = list((await db.scalars(select(Exercise.id).where(Exercise.app_user_id==owner.id))).all())
        blocks = list((await db.scalars(select(TrainingBlock.id).where(TrainingBlock.app_user_id==owner.id).order_by(TrainingBlock.block_index))).all())
        count = await db.scalar(select(func.count()).select_from(WorkoutSessionSet).join(WorkoutSessionExercise).join(WorkoutSession).where(WorkoutSession.app_user_id==owner.id))
        assert count == 100000 and len(exercises)==1 and len(blocks)==2
        claims = {'uid':owner.firebase_uid,'email':owner.email}
    app.dependency_overrides[get_current_firebase_claims] = lambda: claims
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(app,log_level='warning',lifespan='off'))
    server.install_signal_handlers=lambda:None
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    target=Path(args.mobile_root).resolve()
    evidence=target/'docs/validation/p1-08/artifacts/task21'
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.05)
        assert server.started
        env=dict(os.environ,P108_LARGE_HTTP=f'http://127.0.0.1:{sock.getsockname()[1]}',
                 P108_LARGE_EXERCISE=str(exercises[0]),P108_LARGE_BLOCK_A=str(blocks[0]),P108_LARGE_BLOCK_B=str(blocks[1]),
                 P108_LARGE_OUTPUT=str(evidence/'large-http-deadline.json'))
        with (evidence/'large-http-deadline.log').open('wb') as log:
            process=await asyncio.create_subprocess_exec('node','node_modules/jest/bin/jest.js','--runInBand','--runTestsByPath',
                'api/__tests__/analyticsLargeHttp.test.ts','--watchman=false',cwd=target,env=env,stdout=log,stderr=asyncio.subprocess.STDOUT)
            try:await asyncio.wait_for(process.wait(),210)
            except TimeoutError:
                process.kill();await process.wait();raise
        assert process.returncode==0,'Inspect large-http-deadline.log'
        result=json.loads((evidence/'large-http-deadline.json').read_text())
        print(json.dumps(result,indent=2))
    finally:
        server.should_exit=True
        await serving
        sock.close()
        app.dependency_overrides.clear()
        await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--mobile-root',required=True);parser.add_argument('--owner-id',type=int,required=True)
    args=parser.parse_args();configure()
    if urlparse(os.environ['DATABASE_URL']).path!='/eurith_p108_test_benchmark':
        raise SystemExit('Only the separate localhost benchmark database is allowed')
    asyncio.run(main(args))
