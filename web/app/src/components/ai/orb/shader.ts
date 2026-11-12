// Superchat's orb shader. Shader inspired by "Shader Reminder" by Daniela Muntyan (CC BY 4.0);
// original implementation. Ported exactly from the approved mockup (the old design notes "Orb").

export const ORB_VERTEX_SHADER = "attribute vec2 p; void main(){ gl_Position = vec4(p,0.,1.); }";

export const ORB_FRAGMENT_SHADER = `precision highp float;
uniform vec2 r; uniform float t; uniform float energy; uniform float light;
float field(vec2 p, float tt){
  float a = sin(p.x*2.1 + tt*.55 + 1.4*sin(p.y*1.7 - tt*.33));
  float b = sin(p.y*2.4 - tt*.42 + 1.2*sin(p.x*1.3 + tt*.21));
  float c = sin((p.x+p.y)*1.6 + tt*.27);
  return a*.55 + b*.35 + c*.25;
}
void main(){
  vec2 uv = (gl_FragCoord.xy - .5*r) / (.5*r.y);
  float d = length(uv); float R = .9;
  if (d > R + .03) { gl_FragColor = vec4(0.); return; }
  vec2 p = uv * (1.0 + .08*sin(t*.2));
  float f = field(p, t), f2 = field(p*1.35 + 3.1, t*1.1 + 2.0);
  float r1 = exp(-pow((f - .15) * 3.2, 2.)), r2 = exp(-pow((f2 + .25) * 4.0, 2.)) * .6;
  float rim = smoothstep(.25, R, d), body = smoothstep(.95, .05, d);
  float lit = (r1 + r2) * (.25 + 1.1*pow(rim, 1.6)) * (.55 + .45*(1. - body)) + .22*pow(smoothstep(.6, R, d), 5.);
  float ang = atan(uv.y, uv.x);
  vec3 col = mix(vec3(.20,.45,1.), vec3(.56,.40,1.), .5 + .5*sin(ang*1.2 + t*.25));
  col = mix(col, vec3(1.,.40,.75), smoothstep(.1, .8, (-uv.y - uv.x*.4) * .9) * rim);
  col = mix(col, vec3(.62,.82,1.), r1 * .35);
  vec3 core = mix(vec3(.012,.014,.03), vec3(.06,.065,.11), light);
  gl_FragColor = vec4(core + min(col * lit * (.95 + .25*energy), vec3(.92)), smoothstep(R + .03, R - .02, d));
}`;
