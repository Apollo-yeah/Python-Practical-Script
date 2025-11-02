import os
import argparse

def rename_image_files(folder_path, start_num=0, image_extensions=None):
    """
    批量重命名指定文件夹中的图片文件
    
    参数:
        folder_path: 图片所在文件夹路径
        start_num: 起始编号,默认为0
        image_extensions: 要处理的图片扩展名列表,默认为常见图片格式
    """
    # 默认支持的图片格式
    if image_extensions is None:
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.ico'}
    
    # 检查文件夹是否存在
    if not os.path.isdir(folder_path):
        print(f"错误：文件夹 '{folder_path}' 不存在")
        return
    
    # 获取文件夹中所有文件
    all_files = os.listdir(folder_path)
    image_files = []
    
    # 筛选出图片文件
    for file in all_files:
        file_path = os.path.join(folder_path, file)
        # 只处理文件,不处理文件夹
        if os.path.isfile(file_path):
            # 获取文件扩展名（小写）
            ext = os.path.splitext(file)[1].lower()
            if ext in image_extensions:
                image_files.append((file, ext))
    
    if not image_files:
        print(f"在 '{folder_path}' 中未找到任何图片文件")
        return
    
    # 按原文件名排序（确保重命名顺序可预测）
    image_files.sort()
    
    # 执行重命名
    renamed_count = 0
    for i, (old_name, ext) in enumerate(image_files, start=start_num):
        old_path = os.path.join(folder_path, old_name)
        new_name = f"{i}{ext}"
        new_path = os.path.join(folder_path, new_name)
        
        # 避免覆盖已存在的文件
        if os.path.exists(new_path) and old_path != new_path:
            print(f"警告：文件 '{new_name}' 已存在,跳过重命名 '{old_name}'")
            continue
        
        try:
            os.rename(old_path, new_path)
            print(f"重命名: {old_name} -> {new_name}")
            renamed_count += 1
        except Exception as e:
            print(f"重命名失败 '{old_name}': {str(e)}")
    
    print(f"\n操作完成,共重命名 {renamed_count} 个文件")

if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='批量重命名图片文件为数字序号格式')
    parser.add_argument('folder', help='图片所在的文件夹路径')
    parser.add_argument('-s', '--start', type=int, default=0, help='起始编号,默认为0')
    args = parser.parse_args()
    
    # 调用重命名函数
    rename_image_files(args.folder, args.start)