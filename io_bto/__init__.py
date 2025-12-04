import bpy
import json
import os
import mathutils
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, CollectionProperty
from bpy.types import Operator

bl_info = {
    "name": "Genesis (Build To Order) Importer",
    "author": "be1nyu",
    "version": (0, 0, 1),
    "blender": (4, 0, 0),
    "location": "File > Import > Genesis (Build To Order) (.json)",
    "description": "Import Genesis (Build To Order) models into active collection",
    "category": "Import-Export",
    "support": "COMMUNITY",
    "tracker_url": "https://github.com/be1nyu/io_bto/issues"
}

def find_float_arrays(obj, min_len=3):
    results = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list) and all(isinstance(x, (int, float)) for x in v) and len(v) >= min_len:
                results[k] = v
            else:
                results.update(find_float_arrays(v, min_len))
    elif isinstance(obj, list):
        for v in obj:
            results.update(find_float_arrays(v, min_len))
    return results

def search_named_arrays(node):
    found = {}
    if isinstance(node, dict):
        if "name" in node and "data" in node and isinstance(node["data"], list):
            found[node["name"]] = node
        for v in node.values():
            found.update(search_named_arrays(v))
    elif isinstance(node, list):
        for e in node:
            found.update(search_named_arrays(e))
    return found

# 위치/노멀/uv 추출
def detect_attribute(json_root, preferred_names):
    candidates = search_named_arrays(json_root)
    for pname in preferred_names:
        if pname in candidates:
            return candidates[pname]

    arrays = find_float_arrays(json_root, min_len=3)
    for arr in arrays.values():
        if len(arr) % 3 == 0:
            return {"data": arr, "stride": 3}
    return None

# 오브젝트 생성
def build_meshes_from_genesis(context, jdata):
    pos_candidates = ["aPosition", "aPos", "positions", "vertices", "aVertex", "position"]
    norm_candidates = ["aNormal", "normals", "normal"]
    uv_candidates = ["aUV0", "uv", "uvs", "aUV", "texcoords"]

    transform_dict = {t["id"]: t for t in jdata.get("transforms", []) if isinstance(t, dict)}

    def compute_world_transform(transform_id):
        if transform_id not in transform_dict:
            return mathutils.Matrix.Identity(4)

        t = transform_dict[transform_id]
        pos = t.get("position", [0, 0, 0])
        rot = t.get("rotation", [0, 0, 0, 1])
        scl = t.get("scale", [1, 1, 1])

        loc = mathutils.Matrix.Translation((float(pos[0]), float(pos[1]), float(pos[2])))
        quat = mathutils.Quaternion((float(rot[3]), float(rot[0]), float(rot[1]), float(rot[2])))
        rot_mat = quat.to_matrix().to_4x4()
        scl_mat = mathutils.Matrix.Diagonal((float(scl[0]), float(scl[1]), float(scl[2]), 1.0))

        local = loc @ rot_mat @ scl_mat
        parent_id = t.get("parentId", -1)

        if parent_id != -1 and parent_id in transform_dict:
            return compute_world_transform(parent_id) @ local
        return local

    objects = jdata.get("objects") or jdata.get("meshes") or []
    created = []

    for idx, o in enumerate(objects):
        name = o.get("name", f"GenesisObject_{idx}")

        indices = o.get("indices") or o.get("faces") or o.get("triangles")
        if not indices:
            continue

        try:
            indices = [int(x) for x in indices]
        except Exception:
            continue

        pos_attr = detect_attribute(o, pos_candidates)
        if not pos_attr:
            verts = []
            if isinstance(o, dict):
                for k in o:
                    if "position" in k.lower() or "vertex" in k.lower():
                        arr = o[k]
                        if isinstance(arr, list) and len(arr) % 3 == 0:
                            verts = [(arr[i], arr[i+1], arr[i+2]) for i in range(0, len(arr), 3)]
                            break
            if not verts:
                continue
        else:
            pos_data = pos_attr.get("data", [])
            stride = pos_attr.get("stride", 3)
            verts = [(pos_data[i], pos_data[i+1], pos_data[i+2])
                     for i in range(0, len(pos_data), stride) if i + 2 < len(pos_data)]

        max_idx = max(indices) if indices else -1
        if len(verts) <= max_idx:
            continue

        # 면 생성
        faces = []
        for i in range(0, len(indices), 3):
            try:
                a, b, c = indices[i], indices[i+1], indices[i+2]
                if 0 <= a < len(verts) and 0 <= b < len(verts) and 0 <= c < len(verts):
                    faces.append((a, b, c))
            except Exception:
                pass

        if not faces:
            continue

        # 메쉬 생성
        mesh = bpy.data.meshes.new(name + "_mesh")
        try:
            mesh.from_pydata(verts, [], faces)
        except Exception:
            continue
        mesh.update()

        obj = bpy.data.objects.new(name, mesh)
        context.collection.objects.link(obj)

        tid = o.get("transformId")
        if tid in transform_dict:
            wm = compute_world_transform(tid)
            loc, rot, scale = wm.decompose()
            obj.location = loc
            obj.rotation_mode = 'QUATERNION'
            obj.rotation_quaternion = rot
            obj.scale = scale

        # uv 적용
        uv_attr = detect_attribute(o, uv_candidates)
        if uv_attr:
            uv_data = uv_attr.get("data", [])
            stride = uv_attr.get("stride", 2)
            if len(uv_data) >= stride:
                # uv 레이어 생성
                try:
                    uv_layer = mesh.uv_layers.new(name="UVMap")
                    uv_loops = uv_layer.data

                    uv_per_vert = [(uv_data[i], uv_data[i+1])
                                   for i in range(0, len(uv_data), stride)
                                   if i + 1 < len(uv_data)]

                    if uv_per_vert:
                        for poly in mesh.polygons:
                            for li in poly.loop_indices:
                                vi = mesh.loops[li].vertex_index
                                if vi < len(uv_per_vert):
                                    uv_loops[li].uv = uv_per_vert[vi]
                except Exception:
                    pass

        # 노멀 적용
        norm_attr = detect_attribute(o, norm_candidates)
        if norm_attr:
            nd = norm_attr.get("data", [])
            stride = norm_attr.get("stride", 3)

            if len(nd) >= stride:
                loop_normals = []
                for li in mesh.loops:
                    vi = li.vertex_index
                    ni = vi * stride
                    if ni + 2 < len(nd):
                        loop_normals.append((nd[ni], nd[ni+1], nd[ni+2]))
                    else:
                        loop_normals.append((0.0, 0.0, 1.0))
                try:
                    mesh.normals_split_custom_set(loop_normals)
                except Exception:
                    pass

        created.append(obj)

    return created

# 메인
class ImportGenesisBTO(Operator, ImportHelper):
    bl_idname = "import_scene.bto"
    bl_label = "Import Genesis (Build To Order)"
    bl_options = {'PRESET', 'UNDO'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={'HIDDEN'})

    def execute(self, context):
        path = self.filepath
        if not os.path.exists(path):
            self.report({'ERROR'}, "File not found.")
            return {'CANCELLED'}

        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            txt = f.read()
        try:
            jdata = json.loads(txt)
        except Exception:
            try:
                s, e = txt.find('{'), txt.rfind('}')
                if s != -1 and e != -1 and e > s:
                    jdata = json.loads(txt[s:e+1])
                else:
                    raise
            except Exception as err:
                self.report({'ERROR'}, f"JSON parse error: {err}")
                return {'CANCELLED'}

        objs = build_meshes_from_genesis(context, jdata)
        self.report({'INFO'}, f"Imported {len(objs)} object(s)")
        return {'FINISHED'}

def menu_func_import(self, context):
    self.layout.operator(ImportGenesisBTO.bl_idname, text="Genesis (Build To Order) (.json)")

classes = (ImportGenesisBTO,)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
