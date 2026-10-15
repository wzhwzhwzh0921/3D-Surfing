"""Native GLB duplication and camera evidence, on either configured backend."""
import json, os, sys
from pathlib import Path
# Standalone workers are uploaded as snapshots, without package dependencies.
_backend = os.getenv('SCENEQA_RENDER_BACKEND', 'local')
if _backend not in ('local', 'k8s'):
    raise RuntimeError('SCENEQA_RENDER_BACKEND must be local or k8s')
if _backend == 'k8s' and (not os.environ.get('KUBERNETES_SERVICE_HOST') or not Path('/var/run/secrets/kubernetes.io/serviceaccount/namespace').is_file()):
    raise RuntimeError('K8s rendering was selected; this worker must run in Kubernetes. No local fallback.')
os.environ.setdefault('PYOPENGL_PLATFORM','egl')
import numpy as np
import trimesh, pyrender
from PIL import Image, ImageDraw


def pose(center, extent, az, el=25, distance=1.7):
    a,e=np.deg2rad([az,el]); eye=center+extent*distance*np.array([np.sin(a)*np.cos(e),np.sin(e),np.cos(a)*np.cos(e)])
    z=eye-center;z/=np.linalg.norm(z);x=np.cross([0.,1.,0.],z);x/=np.linalg.norm(x);y=np.cross(z,x)
    p=np.eye(4);p[:3,:3]=np.stack([x,y,z],axis=1);p[:3,3]=eye;return p


def shallow_scene(scene,mesh_only=False):
    """Share immutable native geometry/materials; flatten only node transforms."""
    result=trimesh.Scene(base_frame=scene.graph.base_frame)
    result.metadata=dict(scene.metadata)
    for node in scene.graph.nodes_geometry:
        matrix,geometry=scene.graph[node]
        if mesh_only and not isinstance(scene.geometry[geometry],trimesh.Trimesh):continue
        result.geometry[geometry]=scene.geometry[geometry]
        result.graph.update(frame_to=node,matrix=matrix,geometry=geometry)
    return result


def renderable(scene):
    if all(isinstance(g,trimesh.Trimesh) for g in scene.geometry.values()):return scene
    return shallow_scene(scene,mesh_only=True)

def render(scene,out,center,extent,targets=None):
    stage=pyrender.Scene.from_trimesh_scene(renderable(scene),ambient_light=[.9]*3,bg_color=[245,245,245,255]); r=pyrender.OffscreenRenderer(768,768)
    cameras={}
    for zoom,d in [('overview',1.7),('closer',1.15)]:
        for az in (45,135,225,315):
            name=f'az_{az:03d}_{zoom}';p=pose(center,extent,az,distance=d);cam=stage.add(pyrender.PerspectiveCamera(yfov=np.pi/4),pose=p);lamp=stage.add(pyrender.DirectionalLight(color=np.ones(3),intensity=3),pose=p)
            rgb,_=r.render(stage);Image.fromarray(rgb).save(out/f'{name}.png');stage.remove_node(cam);stage.remove_node(lamp);cameras[name]={'matrix':p.tolist(),'position':p[:3,3].tolist(),'target':center.tolist(),'yfov':float(np.pi/4)}
    if targets:
        lo=np.min([tc-te*.5 for tc,te in targets],axis=0);hi=np.max([tc+te*.5 for tc,te in targets],axis=0)
        tc=(lo+hi)/2;te=max(float(max(hi-lo)),extent*.08)
        for az in (45,225):
            name=f'region_{az}';p=pose(tc,te,az,el=25,distance=2.0);cam=stage.add(pyrender.PerspectiveCamera(yfov=np.pi/4),pose=p);lamp=stage.add(pyrender.DirectionalLight(color=np.ones(3),intensity=3),pose=p)
            rgb,_=r.render(stage);Image.fromarray(rgb).save(out/f'{name}.png');stage.remove_node(cam);stage.remove_node(lamp);cameras[name]={'matrix':p.tolist(),'position':p[:3,3].tolist(),'target':tc.tolist(),'yfov':float(np.pi/4)}
    for i,(tc,te) in enumerate((targets or [])[:2]):
        for az in (45,225):
            name=f'detail_{i}_{az}';p=pose(tc,te,az,el=30,distance=2.5);cam=stage.add(pyrender.PerspectiveCamera(yfov=np.pi/4),pose=p);lamp=stage.add(pyrender.DirectionalLight(color=np.ones(3),intensity=3),pose=p)
            rgb,_=r.render(stage);Image.fromarray(rgb).save(out/f'{name}.png');stage.remove_node(cam);stage.remove_node(lamp);cameras[name]={'matrix':p.tolist(),'position':p[:3,3].tolist(),'target':tc.tolist(),'yfov':float(np.pi/4)}
    r.delete();(out/'cameras.json').write_text(json.dumps(cameras))


def local_support_height(scene,node,x,z):
    """A vertical center ray against the selected native support, without rtree."""
    matrix,geometry=scene.graph[node];mesh=scene.geometry[geometry]
    triangles=trimesh.transform_points(mesh.triangles.reshape(-1,3),matrix).reshape(-1,3,3);a=triangles[:,0];u=triangles[:,1]-a;v=triangles[:,2]-a
    qx=x-a[:,0];qz=z-a[:,2];den=u[:,0]*v[:,2]-u[:,2]*v[:,0]
    valid=np.abs(den)>1e-12;b=np.zeros(len(den));c=np.zeros(len(den))
    b[valid]=(qx[valid]*v[valid,2]-qz[valid]*v[valid,0])/den[valid]
    c[valid]=(u[valid,0]*qz[valid]-u[valid,2]*qx[valid])/den[valid]
    hit=valid&(b>=-1e-7)&(c>=-1e-7)&(b+c<=1+1e-7)
    if not np.any(hit):raise ValueError(f'support {node}: no local surface at x={x:.4f},z={z:.4f}; adjust offset or support')
    return float(np.max(a[hit,1]+b[hit]*u[hit,1]+c[hit]*v[hit,1]))

def candidates(scene,inventory,out,center,extent):
    groups={}
    for row in inventory:
        name='_'.join(row['name'].split('_')[:2])
        groups.setdefault(name,[]).append(row)
    selected=[];seen=set()
    for name,rows in groups.items():
        lo=np.min([r['bbox'][0] for r in rows],axis=0);hi=np.max([r['bbox'][1] for r in rows],axis=0)
        if max(hi-lo)>.22 or max(hi-lo)<.008 or min(hi-lo)<.0002:continue
        family=name.split('.')[0].split('_')[0]
        if family in seen:continue
        seen.add(family);selected.append({'name':name,'ids':[r['id'] for r in rows],'bbox':[lo.tolist(),hi.tolist()]})
        if len(selected)>=16:break
    sheet=Image.new('RGB',(4*256,4*280),'white');draw=ImageDraw.Draw(sheet)
    for i,group in enumerate(selected):
        part=trimesh.Scene()
        for ident in group['ids']:
            node=[n for n in scene.graph.nodes_geometry if isinstance(scene.geometry[scene.graph[n][1]],trimesh.Trimesh)][ident];m,g=scene.graph[node];part.add_geometry(scene.geometry[g],transform=m)
        b=part.bounds;st=pyrender.Scene.from_trimesh_scene(part,ambient_light=[.9]*3,bg_color=[245,245,245,255]);p=pose(b.mean(0),float(max(b[1]-b[0])),45,25)
        st.add(pyrender.PerspectiveCamera(yfov=np.pi/4),pose=p);st.add(pyrender.DirectionalLight(color=np.ones(3),intensity=3),pose=p)
        r=pyrender.OffscreenRenderer(256,256);rgb,_=r.render(st);r.delete();x=(i%4)*256;y=(i//4)*280;sheet.paste(Image.fromarray(rgb),(x,y));draw.text((x+4,y+257),str(group['ids'])[:36],fill='black')
    sheet.save(out/'groups.png');(out/'groups.json').write_text(json.dumps(selected,ensure_ascii=False))

def main():
    source,out=Path(sys.argv[1]),Path(sys.argv[2]);out.mkdir(parents=True,exist_ok=True)
    scene=trimesh.load(source,force='scene')
    for geom in scene.geometry.values():
        if not isinstance(geom,trimesh.Trimesh):continue
        material=getattr(geom.visual,'material',None)
        if material is not None:
            for attr in ('baseColorTexture','metallicRoughnessTexture','normalTexture','occlusionTexture','emissiveTexture','image'):
                image=getattr(material,attr,None)
                if getattr(image,'mode',None) in ('LA','1'):setattr(material,attr,image.convert('RGBA'))
    b=np.asarray(scene.bounds);center=b.mean(0);extent=float(np.max(b[1]-b[0]))
    if len(sys.argv)>3 and sys.argv[3]=='--screen-only':render(scene,out,center,extent);return
    nodes=[n for n in scene.graph.nodes_geometry if isinstance(scene.geometry[scene.graph[n][1]],trimesh.Trimesh)];inventory=[];raw_bounds=[]
    for i,node in enumerate(nodes):
        matrix,geo=scene.graph[node];mesh=scene.geometry[geo];points=trimesh.transform_points(mesh.vertices,matrix);world_bounds=np.array([points.min(0),points.max(0)]);del points
        raw_bounds.append(world_bounds)
        inventory.append({'id':i,'name':node,'center':np.round((world_bounds.mean(0)-center)/extent,3).tolist(),'size':np.round((world_bounds[1]-world_bounds[0])/extent,3).tolist(),'bbox':np.round((world_bounds-center)/extent,3).tolist()})
    (out/'inventory.json').write_text(json.dumps(inventory,ensure_ascii=False))
    if len(sys.argv)<4 or sys.argv[3]=='--groups-only':
        candidates(scene,inventory,out,center,extent)
        if len(sys.argv)<4:render(scene,out,center,extent)
        return
    plan=json.loads(Path(sys.argv[3]).read_text())
    for item in plan['items']:
        dest=out/item['level'];dest.mkdir(exist_ok=True);aug=shallow_scene(scene);targets=[]
        for j,copy in enumerate(item['copies']):
            offset=np.array(copy.get('offset',[0,0,0]),dtype=float)*extent
            if copy.get('at') is not None:
                if copy.get('support') is None:raise ValueError('at placement requires support')
                at=np.array(copy['at'],dtype=float);support_bounds=raw_bounds[int(copy['support'])]
                native_center=(np.min([raw_bounds[int(i)][0] for i in copy['ids']],axis=0)+np.max([raw_bounds[int(i)][1] for i in copy['ids']],axis=0))*.5
                destination=support_bounds[0,[0,2]]+at*(support_bounds[1,[0,2]]-support_bounds[0,[0,2]])
                offset[[0,2]]=destination-native_center[[0,2]]
            if copy.get('support') is not None:
                group_center=(np.min([raw_bounds[int(i)][0] for i in copy['ids']],axis=0)+np.max([raw_bounds[int(i)][1] for i in copy['ids']],axis=0))*.5+offset
                try:
                    support=local_support_height(scene,nodes[int(copy['support'])],group_center[0],group_center[2])
                except ValueError as exc:
                    # Suggest real nearby hits; the model still decides placement.
                    bounds=raw_bounds[int(copy['support'])];span=bounds[1,[0,2]]-bounds[0,[0,2]]
                    origin=(group_center[[0,2]]-bounds[0,[0,2]])/np.maximum(span,1e-12);suggestions=[]
                    for delta in ([0,-.1],[0,.1],[-.1,0],[.1,0],[-.1,-.1],[.1,.1]):
                        probe=origin+delta
                        if np.any(probe<0) or np.any(probe>1):continue
                        point=bounds[0,[0,2]]+probe*span
                        try:local_support_height(scene,nodes[int(copy['support'])],point[0],point[1])
                        except ValueError:continue
                        suggestions.append(np.round(probe,4).tolist())
                        if len(suggestions)>=3:break
                    raise ValueError(str(exc)+'; same-support actual-ray at suggestions='+json.dumps(suggestions)) from exc
                floor=min(raw_bounds[int(i)][0][1] for i in copy['ids'])
                offset[1]=support-floor
            lo=np.min([raw_bounds[int(i)][0] for i in copy['ids']],axis=0)+offset
            hi=np.max([raw_bounds[int(i)][1] for i in copy['ids']],axis=0)+offset
            targets.append(((lo+hi)/2,max(float(max(hi-lo)),extent*.05)))
            for k,ident in enumerate(copy['ids']):
                node=nodes[int(ident)];matrix,geo=scene.graph[node];m=matrix.copy()
                angle=float(copy.get('rotation_y',0))*np.pi/180
                pivot=(lo+hi)/2-offset
                rotation=trimesh.transformations.rotation_matrix(angle,[0,1,0],point=pivot)
                m=rotation@m;m[:3,3]+=offset
                aug.graph.update(frame_to=f'qa_copy_{j}_{k}',geometry=geo,matrix=m)
        # A native question must retain the downloaded asset, including metadata
        # and textures that a generic re-export may drop or serialize differently.
        (dest/'scene.glb').write_bytes(aug.export(file_type='glb') if item['copies'] else source.read_bytes())
        render(aug,dest,center,extent,targets)
    print(json.dumps({'status':'complete','nodes':len(nodes)}))
if __name__=='__main__':main()
