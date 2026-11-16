/** @typedef {{id:number,code:string,amount:number,distance:number,holiday:boolean,label:0|1|null,note:string}} Claim */
/** @typedef {{id:number,name:string,threshold:number,state:string,result:object}} Training */
/** @typedef {{id:number,claim_id:number,training_id:number,score:number,decision:string,status:string}} Review */
/** @typedef {{id:number,training_id:number,threshold:number,seed:number,state:'open'|'closed',revision:number,closed_revision:number|null,published_result_id:number|null,population_n:number,sampled_n:number,strata:object[],checksum:string,training_checksum:string,created_at:string}} AuditBatch */
/** @typedef {{id:number,batch_id:number,claim_id:number,code:string,stratum:string,score:number,include_prob:number,basis:object,decision:'pending'|'valid'|'flagged',note:string}} AuditSample */
/** @typedef {{id:number,batch_id:number,revision:number,estimable:boolean,result:object,created_at:string}} AuditResult */
