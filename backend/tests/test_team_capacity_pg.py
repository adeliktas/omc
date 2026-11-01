"""Disposable PostgreSQL admission: account slots and module leases across cycles."""
import asyncio
import json
import os
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.boards import Board
from app.models.gateways import Gateway
from app.models.organizations import Organization
from app.models.tasks import Task
from app.models.team import TeamCycle
from app.services import team_cycle as svc
from tests.test_team_cycle import BASE, RANKING


@pytest.mark.parametrize('scopes,capacity,expected',[
    (('backend','frontend'),1,1),
    (('backend','backend/api'),4,1),
    (('backend','frontend'),4,2),
])
async def test_atomic_fleet_admission(scopes,capacity,expected):
    url=os.getenv('MC_TEST_POSTGRES_URL')
    if not url:pytest.skip('requires migrated disposable /mc_audit')
    assert url.endswith('/mc_audit'), 'Refusing non-disposable database'
    engine=create_async_engine(url)
    async with engine.begin() as conn:await conn.execute(text('TRUNCATE organizations CASCADE'))
    ranking=deepcopy(RANKING)
    for e in ranking['levels'][0]['entries']:e['max_concurrency']=capacity
    ids=[]
    async with AsyncSession(engine,expire_on_commit=False) as s:
        org=Organization(id=uuid4(),name='capacity');s.add(org);await s.flush()
        gateway=Gateway(id=uuid4(),organization_id=org.id,name='g',url='https://g.invalid',workspace_root='/tmp/w');s.add(gateway);await s.flush()
        board=Board(id=uuid4(),organization_id=org.id,name='b',slug=uuid4().hex,gateway_id=gateway.id);s.add(board);await s.flush()
        member=await svc.recruit(s,board,'developer',ranking,engine=('claude-code','sonnet'),created_by='admin')
        for scope in scopes:
            task=Task(id=uuid4(),board_id=board.id,title='module',status='in_progress',execution_lane='harness',execution_mode='team');s.add(task);await s.flush()
            cycle,_=await svc.start_cycle(s,board,task,ranking)
            for a in await svc._attempts(s,cycle.id):a.status='succeeded'
            cycle.state='developing'
            await svc._new_attempt(s,cycle,member,'dev-module',assignment={'scope':[scope]})
            ids.append(cycle.id)
        await s.commit()
    async def take(cid):
        async with AsyncSession(engine,expire_on_commit=False) as s:
            c=await s.get(TeamCycle,cid)
            got=await svc.claim_next(s,c,base_sha=BASE,ranking=ranking)
            await s.commit()
            return bool(got)
    assert sum(await asyncio.gather(*(take(cid) for cid in ids)))==expected
    await engine.dispose()
