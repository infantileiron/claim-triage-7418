from sqlalchemy import Column,Integer,String,Float,JSON,Text,ForeignKey,Boolean,UniqueConstraint
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True);password=Column(String(200));role=Column(String(20))
class Claim(Base):
 __tablename__='claims'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True,nullable=False);amount=Column(Float);distance=Column(Float);holiday=Column(Boolean);label=Column(Integer,nullable=True);note=Column(Text,default='')
class Training(Base):
 __tablename__='trainings'
 id=Column(Integer,primary_key=True);name=Column(String(100));threshold=Column(Float);state=Column(String(20),default='queued');result=Column(JSON);error=Column(Text,default='')
class Review(Base):
 __tablename__='reviews'
 __table_args__=(UniqueConstraint('training_id','claim_id',name='uq_review_source'),)
 id=Column(Integer,primary_key=True);training_id=Column(Integer,ForeignKey('trainings.id'));claim_id=Column(Integer,ForeignKey('claims.id'));score=Column(Float);decision=Column(String(30),default='pending');note=Column(Text,default='');status=Column(String(20),default='draft')
class AuditBatch(Base):
 __tablename__='audit_batches'
 __table_args__=(UniqueConstraint('open_key',name='uq_audit_batch_open'),)
 id=Column(Integer,primary_key=True);training_id=Column(Integer,ForeignKey('trainings.id'),nullable=False)
 threshold=Column(Float);seed=Column(Integer);strata=Column(JSON);population=Column(JSON)
 population_n=Column(Integer,default=0);sampled_n=Column(Integer,default=0)
 state=Column(String(20),default='open');revision=Column(Integer,default=0)
 closed_revision=Column(Integer,nullable=True);published_result_id=Column(Integer,nullable=True)
 checksum=Column(String(64));training_checksum=Column(String(64));last_error=Column(Text,default='')
 open_key=Column(String(160),nullable=True);created_by=Column(String(100));created_at=Column(String(40))
class AuditSample(Base):
 __tablename__='audit_samples'
 __table_args__=(UniqueConstraint('batch_id','claim_id',name='uq_audit_sample'),)
 id=Column(Integer,primary_key=True);batch_id=Column(Integer,ForeignKey('audit_batches.id'),nullable=False)
 claim_id=Column(Integer,ForeignKey('claims.id'),nullable=False);claim_code=Column(String(60))
 stratum=Column(String(40));score=Column(Float);include_prob=Column(Float);basis=Column(JSON)
 decision=Column(String(20),default='pending');note=Column(Text,default='')
 created_at=Column(String(40));updated_at=Column(String(40))
class AuditResult(Base):
 __tablename__='audit_results'
 __table_args__=(UniqueConstraint('batch_id','revision',name='uq_audit_result_revision'),)
 id=Column(Integer,primary_key=True);batch_id=Column(Integer,ForeignKey('audit_batches.id'),nullable=False)
 revision=Column(Integer,nullable=False);estimable=Column(Boolean,default=False)
 result=Column(JSON);conclusions_digest=Column(String(64),nullable=False);created_at=Column(String(40))
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
