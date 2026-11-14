// Execute the actual dashboard script with a small DOM/canvas and fetch test double.
// This validates UI behavior, not browser layout or real hardware.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('templates/index.html','utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
const context2d = new Proxy({}, {get:()=>()=>{}});
for (const [,id] of html.matchAll(/id="([^"]+)"/g)) {
 elements.set(id,{textContent:'',value:'',style:{},className:'',getBoundingClientRect:()=>({width:600,height:210}),getContext:()=>context2d});
}
const buttons = Array.from({length:8},()=>({disabled:false}));
let requests=[], fail=false, reject=false, now={mock_mode:true,temperature:24,humidity:48,ir_detected:false,flame_detected:false,led1:false,led2:false,dht_stale:false,inputs_stale:false,pins_bcm:{dht:22,led1:5,led2:6},mqtt_connected:false,cloud_enabled:false,mqtt_status:'disabled',mqtt_pending_leds:[],thingspeak_status:'disabled'};
const sandbox={document:{getElementById:id=>{assert(elements.has(id),id);return elements.get(id)},querySelectorAll:()=>buttons},sessionStorage:{getItem:()=>'',setItem:()=>{}},window:{devicePixelRatio:1,addEventListener:()=>{}},setTimeout:()=>{},AbortSignal:{timeout:()=>undefined},console,fetch:async(url,options)=>{
 requests.push({url,options});
 if(fail)throw Error('offline');
 if(options.method==='POST'){
  if(reject)return {ok:false,json:async()=>({error:'Wrong dashboard token'})};
  const name=url.split('/').pop(), on=JSON.parse(options.body).on; now[name]=on;
  return {ok:true,json:async()=>({on})};
 }
 return {ok:true,json:async()=>({...now})};
}};
vm.createContext(sandbox);vm.runInContext(script,sandbox);
(async()=>{
 await sandbox.refresh();
 assert.equal(elements.get('statusBadge').textContent,'SIMULATION');
 assert.equal(elements.get('mockWarn').style.display,'block');
 assert.equal(elements.get('tempVal').textContent,'24.0 °C');
 assert.equal(elements.get('cloudPill').textContent,'ADAFRUIT DISABLED');
 elements.get('token').value='dummy-token';
 await sandbox.setLED('led1',true);
 assert.equal(now.led1,true);assert.equal(now.led2,false);
 const post=requests.find(r=>r.options.method==='POST');
 assert.equal(post.options.headers['X-Dashboard-Token'],'dummy-token');
 assert.equal(post.options.body,'{"on":true}');
 reject=true;await sandbox.setLED('led2',true);
 assert.equal(now.led2,false);assert.equal(elements.get('notice').textContent,'Wrong dashboard token');
 reject=false;await sandbox.setBoth(false);assert.equal(now.led1,false);assert.equal(now.led2,false);
 now.inputs_stale=true;now.dht_stale=true;await sandbox.refresh();
 assert.equal(elements.get('irVal').textContent,'UNAVAILABLE');assert.equal(elements.get('dhtPill').textContent,'STALE');
 fail=true;await sandbox.refresh();
 assert.equal(elements.get('statusBadge').textContent,'DISCONNECTED');
 assert.equal(elements.get('led1Val').textContent,'UNKNOWN');assert.equal(elements.get('cloudPill').textContent,'UNKNOWN');
 assert(buttons.every(b=>b.disabled));
 fail=false;await sandbox.refresh();assert(buttons.every(b=>!b.disabled));
 console.log('PASS: dashboard script simulation, authenticated commands, independent LEDs, errors, stale sensors, disconnect/recovery');
})().catch(e=>{console.error(e);process.exitCode=1});
