from pydantic import BaseModel,Field,field_validator
from typing import Optional,Literal,List
class Login(BaseModel):email:str;password:str
class ClaimInput(BaseModel):
 code:str=Field(min_length=1,max_length=60)
 amount:float=Field(ge=0,le=100000,allow_inf_nan=False)
 distance:float=Field(ge=0,le=10000,allow_inf_nan=False)
 holiday:bool=False
 label:Optional[Literal[0,1]]=None
 note:str=Field(default='',max_length=1000)
class TrainingInput(BaseModel):
 name:str=Field(min_length=2,max_length=100)
 threshold:float=Field(default=.5,ge=.1,le=.9,allow_inf_nan=False)
class ReviewInput(BaseModel):
 decision:Literal['valid','flagged']
 note:str=Field(min_length=6,max_length=2000)
class StratumInput(BaseModel):
 lo:float=Field(ge=0,le=1,allow_inf_nan=False)
 hi:float=Field(gt=0,le=1,allow_inf_nan=False)
 sample_n:int=Field(ge=0,le=10000)
 @field_validator('hi')
 @classmethod
 def bounds(cls,v,info):
  if 'lo' in info.data and v<=info.data['lo']:raise ValueError('分层区间必须满足 lo < hi')
  return v
class AuditBatchInput(BaseModel):
 threshold:float=Field(ge=0,le=1,allow_inf_nan=False)
 seed:int=Field(default=3106,ge=0,le=2**31-1)
 strata:Optional[List[StratumInput]]=None
 default_sample_n:int=Field(default=3,ge=0,le=10000)
class AuditDecisionInput(BaseModel):
 decision:Literal['valid','flagged']
 note:str=Field(min_length=6,max_length=2000)
