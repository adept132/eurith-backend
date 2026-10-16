"""Local-only authenticated HTTP benchmark. See run_p108_tests.py for DB setup.

Default: 10k/100k actual selected sets, two runs, 30 sequential requests per case.
--sizes 1000 --requests 1 --runs 1 is a bounded diagnostic preflight, not acceptance.
"""
import argparse
import asyncio
import json
import hashlib
import math
import statistics
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse

from run_p108_tests import configure


def payload_profile(data, case):
    """Untimed structural accounting; lists are retained, never truncated."""
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    def exercise(value):
        points = value['points']
        return {'points':len(points), 'observations':sum(len(p['details']['observations']) for p in points),
                'points_bytes':size(points), 'record_events_bytes':size(value['record_events']),
                'observation_bytes':sum(size(p['details']['observations']) for p in points)}
    if case == 'exercise':
        return exercise(data)
    if case == 'comparison':
        return {side:{'exercises_bytes':size(data[side]['exercises']),
                      'muscles_with_repeated_source_points_bytes':size(data[side]['muscles']),
                      'exercises':{eid:exercise(value) for eid,value in data[side]['exercises'].items()}}
                for side in ('a', 'b')}
    return {'top_level_type':type(data).__name__}


async def main(args):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import insert, event, delete
    from app.database import SessionLocal, engine
    from api.main import app
    from api.deps import get_current_firebase_claims
    from api.services.models import AppUser, Exercise, TrainingBlock, WorkoutSession, WorkoutSessionExercise, WorkoutSessionSet
    from api.services.analytics.rebuild import rebuild_user
    import os
    os.environ['P108_ANALYTICS_ENABLED']='true'
    output={'kind':'acceptance' if args.requests==30 and args.runs==2 and args.sizes==[10000,100000] else 'diagnostic-preflight',
            'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
            'source_dirty':bool(subprocess.check_output(['git','status','--porcelain'],text=True).strip()),
            'analytics_source_sha256':{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path('api/services/analytics').glob('*.py'))},
            'transport':'HTTP ASGI in-process; real routing, serialization, DB and app-user lookup; external Firebase claims only replaced',
            'fixture':f'One owned exercise; {args.workouts} finished workouts spread over 100 days (half per closed block), all selected sets use saved full-barbell context. All-history exercise and legacy both see every set; comparison union sees every set.',
            'sql_definition':'All SELECT statements including authentication; sequential warmup then measured requests; no concurrency', 'results':[]}
    for size in args.sizes:
        marker='p108-benchmark-'+uuid4().hex
        async with SessionLocal() as db:
            user=AppUser(firebase_uid=marker,email=marker+'@example.invalid',display_name='Local benchmark')
            db.add(user);await db.flush()
            exercise=Exercise(name=marker,category='base',main_muscle_group='chest',secondary_muscle_groups=['triceps'],difficulty='beginner',equipment_needed=[],source='custom',app_user_id=user.id)
            db.add(exercise);await db.flush()
            blocks=[TrainingBlock(app_user_id=user.id,block_index=i,microcycle_length=7,start_date=date(2026,1,1)+timedelta(days=50*i),planned_end_date=date(2026,1,1)+timedelta(days=50*i+49),actual_end_date=date(2026,1,1)+timedelta(days=50*i+49),status='closed') for i in range(2)]
            db.add_all(blocks);await db.flush()
            for index in range(args.workouts):
                stamp=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=index*100/args.workouts)
                w=WorkoutSession(app_user_id=user.id,source='free',status='finished',started_at=stamp,finished_at=stamp+timedelta(hours=1),analytics_snapshot={'schema_version':1,'is_deload':False})
                db.add(w);await db.flush()
                ex=WorkoutSessionExercise(workout_session_id=w.id,exercise_id=exercise.id,order_index=0)
                db.add(ex);await db.flush()
                await db.execute(insert(WorkoutSessionSet),[{'workout_session_exercise_id':ex.id,'set_number':n+1,'set_type':'normal','weight':50+index*10/args.workouts,'reps':8,'is_completed':True,'analytics_load_snapshot':{'schema_version':1,'model':'barbell','input_basis':'full'}} for n in range(size//args.workouts)])
            await db.commit()
            ready=await rebuild_user(db,user.id)
            assert ready['status']=='ready',ready
            uid,eid,bids=user.id,exercise.id,[b.id for b in blocks]
            app.dependency_overrides.clear()
            app.dependency_overrides[get_current_firebase_claims]=lambda:{'uid':marker,'email':marker+'@example.invalid'}
            print(f'Fixture ready: {size} selected sets / {args.workouts} workouts / one exercise',flush=True)
            cases=[('legacy',f'/progress/exercise-history/{eid}',None),('exercise',f'/progress/analytics/exercises/{eid}?period=all',12),('comparison',f'/progress/analytics/blocks/compare?block_a_id={bids[0]}&block_b_id={bids[1]}',20)]
            async with AsyncClient(transport=ASGITransport(app=app),base_url='http://localhost',timeout=None) as client:
                for run in range(1,args.runs+1):
                    for name,path,limit in cases:
                        samples=[];sql=[]
                        def capture(conn,cursor,statement,parameters,context,executemany):
                            if statement.lstrip().upper().startswith('SELECT'):sql.append(statement)
                        event.listen(engine.sync_engine,'before_cursor_execute',capture)
                        try:
                            for index in range(args.requests+1):
                                profile=None
                                if args.profile and index==0 and name=='exercise':
                                    import cProfile
                                    profile=cProfile.Profile();profile.enable()
                                sql.clear();start=time.perf_counter();response=await client.get(path);elapsed=(time.perf_counter()-start)*1000
                                if profile:
                                    import pstats
                                    profile.disable()
                                    with open(str(args.output)+'.profile.txt','w') as handle:pstats.Stats(profile,stream=handle).sort_stats('cumulative').print_stats(65)
                                assert response.status_code==200,(name,response.status_code,response.text[:500])
                                data=response.json()
                                if limit:assert data['state']=='ready' and len(sql)<=limit,(name,len(sql))
                                if index:samples.append({'ms':elapsed,'sql_count':len(sql),'response_bytes':len(response.content)})
                                else:print(f'Warmup {size}/{run}/{name}: {elapsed:.1f} ms; {len(sql)} SELECTs; {len(response.content)} bytes',flush=True)
                        finally:event.remove(engine.sync_engine,'before_cursor_execute',capture)
                        times=sorted(s['ms'] for s in samples)
                        output['results'].append({'fixture_size':size,'selected_sets':size,'selected_workouts':args.workouts,'sets_per_workout':size//args.workouts,'selected_exercises':1,'sets_per_comparison_side':size//2 if name=='comparison' else None,'run':run,'case':name,'requests':args.requests,'sql_count':max(s['sql_count'] for s in samples),'median_ms':statistics.median(times),'p95_ms':times[math.ceil(.95*len(times))-1],'response_bytes':max(s['response_bytes'] for s in samples),'rules_version':data.get('rules_version'),'source_revision':data.get('data_revision'),'samples':samples,'sql':list(sql)})
                        output['results'][-1]['payload_sections'] = payload_profile(data, name)
                        Path(args.output).write_text(json.dumps(output,indent=2),encoding='utf-8')
                        print(f'Measured {size}/{run}/{name}: {args.requests} requests',flush=True)
            # Retain synthetic fixtures when requested for inspection. Large FK
            # cascade cleanup is outside the timed request and may be expensive.
            if args.keep_fixtures:
                print(f'Retained synthetic fixture owner id {uid} for local inspection', flush=True)
                continue
            # Delete exactly this synthetic owner; retain no user history from other tests.
            await db.execute(delete(WorkoutSessionSet).where(WorkoutSessionSet.workout_session_exercise_id.in_(
                __import__('sqlalchemy').select(WorkoutSessionExercise.id).join(WorkoutSession,WorkoutSession.id==WorkoutSessionExercise.workout_session_id).where(WorkoutSession.app_user_id==uid))))
            await db.execute(delete(WorkoutSessionExercise).where(WorkoutSessionExercise.workout_session_id.in_(__import__('sqlalchemy').select(WorkoutSession.id).where(WorkoutSession.app_user_id==uid))))
            await db.execute(delete(WorkoutSession).where(WorkoutSession.app_user_id==uid))
            await db.execute(delete(AppUser).where(AppUser.id==uid));await db.commit()
        app.dependency_overrides.clear()
    await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--sizes',nargs='+',type=int,default=[10000,100000]);parser.add_argument('--requests',type=int,default=30);parser.add_argument('--runs',type=int,default=2);parser.add_argument('--output',required=True)
    parser.add_argument('--profile',action='store_true')
    parser.add_argument('--workouts',type=int,default=100)
    parser.add_argument('--keep-fixtures',action='store_true')
    args=parser.parse_args()
    if args.workouts<2 or args.workouts%2 or any(n<args.workouts or n%args.workouts for n in args.sizes) or args.requests<1 or args.runs<1:parser.error('positive requests/runs, even workouts, and sizes divisible by workouts required')
    configure()
    import os
    if urlparse(os.environ['TEST_DATABASE_URL']).path != '/eurith_p108_test_benchmark':
        raise SystemExit('Refused benchmark: use only eurith_p108_test_benchmark')
    asyncio.run(main(args))
