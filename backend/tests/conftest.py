import os,sys,tempfile
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('JWT_SECRET','test-secret-3106')
_db=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False).name
os.environ['DATABASE_URL']='sqlite+pysqlite:///'+_db
os.environ.setdefault('REDIS_URL','redis://localhost:6379/15')
import pytest
from src.config.database import Base,engine,Session
from src.models import entities  # noqa: F401

@pytest.fixture()
def db_session():
    import src.models.entities as entities
    Base.metadata.drop_all(engine);Base.metadata.create_all(engine)
    s=Session()
    try:yield s
    finally:s.close()

@pytest.fixture()
def api_client(db_session):
    from fastapi.testclient import TestClient
    from src.main import app
    from src.models.entities import User,Claim
    from src.utils.security import hash_password
    for role in ('admin','analyst','viewer'):
        db_session.add(User(email=role+'@t.local',role=role,password=hash_password('Demo123!')))
    db_session.commit()
    with TestClient(app,raise_server_exceptions=False) as c:
        yield _Api(c)

class _Api:
    def __init__(self,c):self.c=c;self.tokens={}
    def login(self,role):
        r=self.c.post('/auth/login',json={'email':role+'@t.local','password':'Demo123!'})
        assert r.status_code==200,r.text
        self.tokens[role]=r.json()['token']
        return self.tokens[role]
    def headers(self,role):
        tok=self.tokens.get(role) or self.login(role)
        return {'Authorization':'Bearer '+tok}
    def req(self,role,method,path,**kw):
        return self.c.request(method,path,headers=self.headers(role),**kw)
