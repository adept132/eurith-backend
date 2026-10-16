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
import time
from urllib.parse import urlparse

B = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(B/'scripts'))
from run_p108_tests import configure


async def main(args):
    import uvicorn
    from sqlalchemy import select, func, event
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
        workout_count=await db.scalar(select(func.count()).select_from(WorkoutSession).where(WorkoutSession.app_user_id==owner.id))
        side_counts=dict((await db.execute(select(WorkoutSession.training_block_id,func.count()).select_from(WorkoutSessionSet).join(WorkoutSessionExercise).join(WorkoutSession).where(WorkoutSession.app_user_id==owner.id).group_by(WorkoutSession.training_block_id))).all())
        claims = {'uid':owner.firebase_uid,'email':owner.email}
    app.dependency_overrides[get_current_firebase_claims] = lambda: claims
    request_stats=[]
    sql=[]
    def capture_sql(conn,cursor,statement,params,context,many):
        if statement.lstrip().upper().startswith('SELECT'):sql.append(statement)
    async def observed_app(scope,receive,send):
        sql.clear();start=time.perf_counter()
        await app(scope,receive,send)
        request_stats.append({'path':scope.get('path'),'sql_count':len(sql),'sql':list(sql),'asgi_seconds':time.perf_counter()-start})
    if args.compact:event.listen(engine.sync_engine,'before_cursor_execute',capture_sql)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    server=uvicorn.Server(uvicorn.Config(observed_app if args.compact else app,log_level='warning',lifespan='off'))
    server.install_signal_handlers=lambda:None
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    target=Path(args.mobile_root).resolve()
    evidence=target/'docs/validation/p1-08/artifacts/task21'
    stem='compact-http' if args.compact else 'large-http-deadline'
    from api.routers import analytics as analytics_routes
    original_pack=analytics_routes.pack_resource
    packing=[]
    def measured_pack(resource,kind,uid):
        start=time.perf_counter();result=original_pack(resource,kind,uid)
        packing.append({'resource':kind,'seconds':time.perf_counter()-start})
        return result
    if args.compact:analytics_routes.pack_resource=measured_pack
    try:
        for _ in range(100):
            if server.started:break
            await asyncio.sleep(.05)
        assert server.started
        env=dict(os.environ,P108_LARGE_HTTP=f'http://127.0.0.1:{sock.getsockname()[1]}',
                 P108_LARGE_EXERCISE=str(exercises[0]),P108_LARGE_BLOCK_A=str(blocks[0]),P108_LARGE_BLOCK_B=str(blocks[1]),
                 P108_LARGE_OWNER=owner.firebase_uid,P108_COMPACT_HTTP='1' if args.compact else '',
                 P108_COMPACT_ASSET_PREFIX=str(evidence/'compact-large-'),
                 P108_LARGE_OUTPUT=str(evidence/(stem+'.json')))
        with (evidence/(stem+'.log')).open('wb') as log:
            process=await asyncio.create_subprocess_exec('node','node_modules/jest/bin/jest.js','--runInBand','--runTestsByPath',
                'api/__tests__/analyticsCompactHttp.test.ts' if args.compact else 'api/__tests__/analyticsLargeHttp.test.ts','--watchman=false',cwd=target,env=env,stdout=log,stderr=asyncio.subprocess.STDOUT)
            try:await asyncio.wait_for(process.wait(),300 if args.compact else 210)
            except TimeoutError:
                process.kill();await process.wait();raise
        assert process.returncode==0,f'Inspect {stem}.log'
        result=json.loads((evidence/(stem+'.json')).read_text())
        if args.compact:
            result['server_pack_conversion']=packing
            result['server_requests']=request_stats
            result['fixture']={'selected_sets':count,'selected_exercises':len(exercises),'selected_workouts':workout_count,'comparison_sets_by_block':side_counts,'database':'eurith_p108_test_benchmark'}
            (evidence/(stem+'.json')).write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2))
    finally:
        server.should_exit=True
        await serving
        sock.close()
        app.dependency_overrides.clear()
        analytics_routes.pack_resource=original_pack
        if args.compact:event.remove(engine.sync_engine,'before_cursor_execute',capture_sql)
        await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--mobile-root',required=True);parser.add_argument('--owner-id',type=int,required=True)
    parser.add_argument('--compact',action='store_true')
    args=parser.parse_args();configure()
    if urlparse(os.environ['DATABASE_URL']).path!='/eurith_p108_test_benchmark':
        raise SystemExit('Only the separate localhost benchmark database is allowed')
    asyncio.run(main(args))
