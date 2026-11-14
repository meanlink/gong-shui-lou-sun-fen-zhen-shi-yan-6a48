/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string,revoked:boolean,revoke_reason:string,revoked_by:string,revoked_at:number}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean,revoked:boolean,revoke_reason:string,revoked_at:number}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean,auto_paused:boolean}} ReportSchedule */
/** @typedef {{id:number,report_id:number,created_at:number,checksum:string,filename:string,destination:string,invalidated:boolean,invalidate_reason:string,invalidated_at:number}} Delivery */
export {};
