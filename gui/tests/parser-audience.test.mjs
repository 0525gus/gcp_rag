import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../public/console/app.js',import.meta.url),'utf8');
const block=source.slice(source.indexOf('  const drawerParserOptions ='),source.indexOf('  function splitIds('));
for(const base of ['enableDocaiFallback','enableImageOcr']) {
 test(`${base}: saving staff off preserves student setting`,async()=>{
  const nodes=new Map();const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',disabled:false,dataset:{},textContent:''});return nodes.get(id)};
  const config={code:'cs',name:'CS',configRevision:'rev',enableDocaiFallbackStaff:true,enableDocaiFallbackStudent:true,enableImageOcrStaff:true,enableImageOcrStudent:true};
  const calls=[];const state={selectedCode:'cs',drawerDocaiRequest:0};
  const ui=vm.runInNewContext(`${block}\n({drawerParserOptions,loadDrawerDocai,updateDrawerDocaiChoice,saveDrawerDocai})`,{$,state,api:async(url,args)=>{if(!args)return config;calls.push({url,args});return {deployment:{runId:'run'}}},closeDrawer(){},renderMcpDeployment(){},showMcpDeploymentModal(){},pollMcpDeployment(){},toast(){}});
  await ui.loadDrawerDocai('cs');
  const option=ui.drawerParserOptions.find(o=>o.key===base+'Staff');
  $(option.select).value='false';ui.updateDrawerDocaiChoice();
  await ui.saveDrawerDocai(option);
  assert.equal(calls.length,1);
  assert.equal(calls[0].args.body[base+'Staff'],false);
  assert.equal(calls[0].args.body[base+'Student'],undefined);
  assert.equal(calls[0].args.body.configRevision,'rev');
 });
}
