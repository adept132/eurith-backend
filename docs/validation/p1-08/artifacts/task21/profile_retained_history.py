"""One diagnostic request per resource; no fixtures or production writes.

Only the dedicated local benchmark database and an existing synthetic owner.
Profiled times are not acceptance timing or a replacement for two x 30 runs.
"""
import argparse
import asyncio
import cProfile
from collections import Counter
import gc
import json
import os
from pathlib import Path
import pstats
import sys
import time
from urllib.parse import urlparse

B=Path(__file__).resolve().parents[5]
sys.path.insert(0,str(B/'scripts'))
from run_p108_tests import configure


def field_accounting(data,case):
    fields=Counter();contexts={key:set() for key in ('load_context','session_context','at','observation_date','intensity')};count=0
    resources=[data] if case=='exercise' else [e for side in ('a','b') for e in data[side]['exercises'].values()]
    for resource in resources:
        for point in resource['points']:
            for row in point['details']['observations']:
                count+=1
                for key,value in row.items():
                    encoded=json.dumps(value,ensure_ascii=False,separators=(',',':'))
                    fields[key]+=len((json.dumps(key)+':'+encoded+',').encode('utf8'))
                    if key in contexts:contexts[key].add(encoded)
    return {'unique_exercise_observations':count,'field_bytes_including_key_colon_comma':dict(fields.most_common()),
            'distinct_context_values':{key:len(values) for key,values in contexts.items()},
            'note':'Counts exercise observation lists once; comparison muscle duplicates excluded from these field totals but retained in response bytes.'}


async def main(args):
    from httpx import ASGITransport,AsyncClient
    from sqlalchemy import select,func
    from app.database import SessionLocal,engine
    from api.main import app
    from api.deps import get_current_firebase_claims
    from api.services.models import AppUser,Exercise,TrainingBlock,WorkoutSession,WorkoutSessionExercise,WorkoutSessionSet
    os.environ['P108_ANALYTICS_ENABLED']='true'
    async with SessionLocal() as db:
        owner=await db.get(AppUser,args.owner_id)
        assert owner and owner.firebase_uid.startswith('p108-benchmark-')
        exercises=list((await db.scalars(select(Exercise.id).where(Exercise.app_user_id==owner.id))).all())
        blocks=list((await db.scalars(select(TrainingBlock.id).where(TrainingBlock.app_user_id==owner.id).order_by(TrainingBlock.block_index))).all())
        count=await db.scalar(select(func.count()).select_from(WorkoutSessionSet).join(WorkoutSessionExercise).join(WorkoutSession).where(WorkoutSession.app_user_id==owner.id))
        assert count==100000 and len(exercises)==1 and len(blocks)==2
        claims={'uid':owner.firebase_uid,'email':owner.email}
    app.dependency_overrides[get_current_firebase_claims]=lambda:claims
    output={'kind':'diagnostic-single-request-no-percentiles' if args.unprofiled else 'diagnostic-profile-only','selected_sets':count,'results':[]}
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://localhost',timeout=None) as client:
            cases=[('exercise',f'/progress/analytics/exercises/{exercises[0]}?period=all'),('comparison',f'/progress/analytics/blocks/compare?block_a_id={blocks[0]}&block_b_id={blocks[1]}')]
            if args.unprofiled:cases.insert(0,('legacy',f'/progress/exercise-history/{exercises[0]}'))
            if args.case:cases=[case for case in cases if case[0]==args.case]
            for name,path in cases:
                if args.compact:path+='&encoding=compact-v1'
                profile=cProfile.Profile();start=time.perf_counter()
                if not args.unprofiled:profile.enable()
                response=await client.get(path)
                profile.disable();elapsed=time.perf_counter()-start
                if not args.unprofiled:
                    with open(f'{args.output}.{name}.profile.txt','w') as log:pstats.Stats(profile,stream=log).sort_stats('cumulative').print_stats(100)
                assert response.status_code==200
                data=response.json()
                if name != 'legacy':assert (data['payload'] if args.compact else data)['state']=='ready'
                output['results'].append({'case':name,'seconds_not_acceptance':elapsed,'profiled':not args.unprofiled,'response_bytes':len(response.content),**({} if args.unprofiled or args.skip_accounting else field_accounting(data,name))})
                Path(args.output).write_text(json.dumps(output,indent=2))
                print(json.dumps(output['results'][-1]),flush=True)
                del data,response;gc.collect()
    finally:
        app.dependency_overrides.clear();await engine.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--owner-id',type=int,required=True);parser.add_argument('--output',required=True)
    parser.add_argument('--unprofiled',action='store_true')
    parser.add_argument('--case',choices=['exercise','comparison'])
    parser.add_argument('--compact',action='store_true')
    parser.add_argument('--skip-accounting',action='store_true')
    args=parser.parse_args();configure()
    if urlparse(os.environ['DATABASE_URL']).path!='/eurith_p108_test_benchmark':raise SystemExit('Dedicated local benchmark DB required')
    asyncio.run(main(args))
