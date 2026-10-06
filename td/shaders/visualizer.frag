// Native TouchDesigner GLSL TOP. Analytic geometry; no external textures.
uniform vec4 uAudio; // bass, mid, high, RMS energy
uniform vec4 uClock; // time, camera speed, symmetry, scene
uniform vec4 uModes; // particle mode, palette, aspect, kick
out vec4 fragColor;
const float PI=3.14159265359;
mat2 rot(float a){float c=cos(a),s=sin(a);return mat2(c,-s,s,c);}
float hash(float n){return fract(sin(n*127.1)*43758.5453);}
vec3 ink(float phase){
 vec3 a=vec3(.20,.65,1.),b=vec3(.85,.15,1.);
 int k=int(uModes.y+.5);
 if(k==1){a=vec3(.5,1.,.12);b=vec3(.04,.75,.55);}
 if(k==2){a=vec3(1.,.64,.16);b=vec3(.95,.08,.23);}
 if(k==3){a=vec3(.92,.96,1.);b=vec3(.3,.4,.53);}
 if(k==4){a=vec3(1.,.12,.18);b=vec3(1.,.7,.15);}
 return mix(a,b,.5+.5*sin(phase));
}
float glow(float d,float w){return exp(-abs(d)/w);}
vec3 temple(vec2 p,float t,bool liquid){
 float n=max(3.,floor(uClock.z));
 float r=length(p),a=atan(p.y,p.x)+t*.04;
 a=abs(mod(a+PI/n,2.*PI/n)-PI/n);
 vec2 q=r*vec2(cos(a),sin(a));
 vec3 col=vec3(0.);
 for(int i=0;i<7;i++){
  float f=float(i),scale=1.4+f*.63;
  vec2 v=rot(t*.035+f*.3)*q*scale;
  float wave=sin(v.x*5.+t*.19+f)*cos(v.y*5.-t*.13);
  float rings=sin(r*(17.+f*2.)-t*(.5+uAudio.x*.5)+wave*(liquid?3.:.8));
  float d=abs(rings)-(.035+.06*uAudio.x);
  float filigree=glow(d,.028+uAudio.z*.022)*(.13+.11*f);
  float spokes=glow(sin(a*n*2.+r*3.+f),.045)*.026;
  col+=ink(f*.61+r*3.+t*.04)*(filigree+spokes)/(1.+r*.7);
 }
 col+=ink(t*.1)*glow(r-.15-uAudio.x*.04,.009)*.75;
 return col*(1.-smoothstep(.1,1.35,r));
}
vec3 tunnel(vec2 p,float t){
 float r=max(length(p),.035),a=atan(p.y,p.x);
 float z=1./r;
 float speed=.55+uClock.y*1.3;
 float lane=a*(5.+floor(uClock.z))+.28*sin(z*.8-t*.2);
 float travel=z-t*speed;
 float rings=glow(sin(travel*2.5),.036+.025*uAudio.x);
 float ribs=glow(sin(lane),.035)*(.25+.15*sin(travel*3.));
 float cells=glow(sin(lane+travel*.8),.055)*.22;
 vec3 col=ink(z*.16+a*.2+t*.05)*(rings*.9+ribs*1.7+cells);
 col*=smoothstep(.025,.16,r)*(1.-smoothstep(.5,1.65,r));
 col+=ink(t*.1)*glow(r-.08,.025)*(.15+uAudio.x*.8);
 return col;
}
vec3 particles(vec2 p,float t){
 vec3 col=vec3(0.);
 int mode=int(uModes.x+.5);
 if(mode==4)return col;
 for(int i=0;i<70;i++){
  float f=float(i),h=hash(f+3.),h2=hash(f+127.);
  float z=fract(h+t*(.014+.018*uClock.y));
  float angle=h2*PI*2.+t*.055*(1.+h);
  float rad=(.10+.8*hash(f+56.))*(.35+z*1.6);
  vec2 pos=vec2(cos(angle),sin(angle))*rad;
  if(mode==0){pos=rot(z*4.+t*.09)*pos;pos.y*=.58;}
  if(mode==1){pos*=.6+fract(t*.18+h)*.9;}
  if(mode==2){pos=vec2((h2-.5)*2.6,1.-fract(z+t*.06)*2.);}
  if(mode==3){pos=rot(t*.06)*pos;pos.y*=.38+.6*h;}
  float d=length(p-pos),size=.0015+z*.003;
  float star=glow(d,size)*1.8+glow(d,size*5.)*.12;
  star*=.3+z*z*(.8+uAudio.z*1.2);
  col+=ink(h2*6.+t*.03)*star;
 }
 float r=length(p*vec2(.7,1.));
 col+=ink(r*3.+t*.04)*exp(-r*6.)*.06;
 return col;
}
void main(){
 vec2 p=(vUV.st-.5)*2.;p.x*=uModes.z;
 float t=uClock.x;
 p*=1.-uAudio.x*.07;
 int scene=int(uClock.w+.5);
 vec3 col;
 if(scene==0)col=temple(p,t,false);
 else if(scene==1)col=tunnel(rot(t*.025)*p,t);
 else if(scene==2)col=particles(p,t);
 else if(scene==3)col=temple(rot(t*.06)*p,t,true)+particles(p,t)*.3;
 else col=tunnel(p,t)*.65+temple(p,t,true)*.5;
 col*=.65+uAudio.w*.8;
 col+=vec3(.0015,.002,.006);
 col=pow(1.-exp(-col*2.3),vec3(.72));
 fragColor=TDOutputSwizzle(vec4(col,1.));
}
