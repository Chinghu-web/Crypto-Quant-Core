#!/usr/bin/env python3
"""
OKX自动交易依赖检查脚本 - Mac版本
专门为macOS优化
"""

import sys
import subprocess
import os

def run_command(cmd):
    """运行shell命令并返回结果"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return -1, "", str(e)

def check_python_version():
    """检查Python版本"""
    print("="*60)
    print("🐍 检查Python版本")
    print("="*60)
    
    # 检查python3
    code, stdout, stderr = run_command("python3 --version")
    if code == 0:
        version = stdout.strip()
        print(f"✅ {version}")
        
        # 提取版本号
        import re
        match = re.search(r'(\d+)\.(\d+)', version)
        if match:
            major, minor = int(match.group(1)), int(match.group(2))
            if major >= 3 and minor >= 8:
                print(f"✅ Python版本符合要求 (需要3.8+)")
                return True, "python3"
            else:
                print(f"⚠️ Python版本过低，建议升级到3.8+")
                return False, "python3"
    
    # 检查python
    code, stdout, stderr = run_command("python --version")
    if code == 0:
        version = stdout.strip()
        print(f"Python命令: {version}")
    
    return False, None

def check_pip():
    """检查pip版本"""
    print("\n" + "="*60)
    print("📦 检查pip版本")
    print("="*60)
    
    for pip_cmd in ["pip3", "pip"]:
        code, stdout, stderr = run_command(f"{pip_cmd} --version")
        if code == 0:
            print(f"✅ {pip_cmd}: {stdout.strip()}")
            return pip_cmd
    
    print("❌ 未找到pip")
    return None

def check_ccxt(pip_cmd):
    """检查ccxt是否安装"""
    print("\n" + "="*60)
    print("🔍 检查ccxt库")
    print("="*60)
    
    try:
        import ccxt
        print(f"✅ ccxt已安装 (版本: {ccxt.__version__})")
        return True
    except ImportError:
        print("❌ ccxt未安装")
        print(f"\n💡 安装命令:")
        print(f"   {pip_cmd} install ccxt")
        print(f"   或")
        print(f"   {pip_cmd} install --user ccxt")
        return False

def check_yaml():
    """检查PyYAML是否安装"""
    print("\n" + "="*60)
    print("📄 检查PyYAML库")
    print("="*60)
    
    try:
        import yaml
        print(f"✅ PyYAML已安装")
        return True
    except ImportError:
        print("❌ PyYAML未安装")
        return False

def check_other_deps(pip_cmd):
    """检查其他依赖"""
    print("\n" + "="*60)
    print("📚 检查其他依赖")
    print("="*60)
    
    deps = {
        'pandas': 'pandas',
        'numpy': 'numpy',
        'requests': 'requests',
        'anthropic': 'anthropic',
        'sqlite3': None  # 内置模块
    }
    
    missing = []
    
    for module_name, package_name in deps.items():
        try:
            __import__(module_name)
            print(f"✅ {module_name}")
        except ImportError:
            if package_name:
                print(f"❌ {module_name}")
                missing.append(package_name)
            else:
                print(f"⚠️ {module_name} (应该是内置的)")
    
    if missing:
        print(f"\n💡 安装缺失的依赖:")
        print(f"   {pip_cmd} install {' '.join(missing)}")
    
    return len(missing) == 0

def check_okx_support():
    """检查OKX交易所支持"""
    print("\n" + "="*60)
    print("🏦 检查OKX支持")
    print("="*60)
    
    try:
        import ccxt
        
        if 'okx' in ccxt.exchanges:
            print("✅ ccxt支持OKX交易所")
            
            # 尝试创建实例
            try:
                exchange = ccxt.okx()
                print("✅ OKX实例创建成功")
                return True
            except Exception as e:
                print(f"⚠️ 创建OKX实例失败: {e}")
                return False
        else:
            print("❌ ccxt不支持OKX交易所（需要更新ccxt）")
            return False
    except ImportError:
        print("❌ 无法导入ccxt")
        return False

def check_config():
    """检查config.yaml"""
    print("\n" + "="*60)
    print("⚙️ 检查config.yaml")
    print("="*60)
    
    if not os.path.exists('config.yaml'):
        print("❌ 未找到config.yaml文件")
        print("   请确保在项目根目录运行此脚本")
        return False
    
    try:
        import yaml
        with open('config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        print("✅ config.yaml加载成功")
        
        # 检查auto_trading配置
        auto_trading = config.get('auto_trading', {})
        enabled = auto_trading.get('enabled', False)
        
        print(f"\n自动交易: {'✅ 已启用' if enabled else '❌ 未启用'}")
        
        if enabled:
            okx = auto_trading.get('okx', {})
            has_key = bool(okx.get('api_key'))
            has_secret = bool(okx.get('secret'))
            has_pass = bool(okx.get('passphrase'))
            
            print(f"  API Key: {'✅' if has_key else '❌ 未设置'}")
            print(f"  Secret: {'✅' if has_secret else '❌ 未设置'}")
            print(f"  Passphrase: {'✅' if has_pass else '❌ 未设置'}")
            
            if not all([has_key, has_secret, has_pass]):
                print("\n⚠️ 请在config.yaml中配置完整的OKX API信息")
        
        return True
        
    except Exception as e:
        print(f"❌ 读取config.yaml失败: {e}")
        return False

def provide_installation_guide(pip_cmd):
    """提供安装指南"""
    print("\n" + "="*60)
    print("📖 Mac安装指南")
    print("="*60)
    
    print("\n🔧 推荐的完整安装步骤:")
    print(f"""
1. 升级pip:
   {pip_cmd} install --upgrade pip

2. 安装所有依赖（一次性）:
   {pip_cmd} install ccxt pandas numpy requests anthropic pyyaml

3. 如果遇到权限问题，使用--user:
   {pip_cmd} install --user ccxt pandas numpy requests anthropic pyyaml

4. 验证ccxt安装:
   python3 -c "import ccxt; print(ccxt.__version__)"

5. 验证OKX支持:
   python3 -c "import ccxt; print('okx' in ccxt.exchanges)"
""")

def main():
    print("🍎 macOS OKX自动交易依赖检查\n")
    
    # 1. 检查Python
    python_ok, python_cmd = check_python_version()
    if not python_ok:
        print("\n❌ Python版本不符合要求")
        print("💡 请安装Python 3.8或更高版本")
        print("   推荐使用Homebrew: brew install python@3.11")
        return
    
    # 2. 检查pip
    pip_cmd = check_pip()
    if not pip_cmd:
        print("\n❌ pip未安装")
        print("💡 请先安装pip:")
        print("   python3 -m ensurepip --upgrade")
        return
    
    # 3. 检查ccxt
    ccxt_ok = check_ccxt(pip_cmd)
    
    # 4. 检查PyYAML
    yaml_ok = check_yaml()
    
    # 5. 检查其他依赖
    deps_ok = check_other_deps(pip_cmd)
    
    # 6. 检查OKX支持
    if ccxt_ok:
        okx_ok = check_okx_support()
    else:
        okx_ok = False
    
    # 7. 检查配置
    config_ok = check_config()
    
    # 总结
    print("\n" + "="*60)
    print("📊 检查总结")
    print("="*60)
    
    all_checks = [
        ("Python版本", python_ok),
        ("pip", pip_cmd is not None),
        ("ccxt", ccxt_ok),
        ("PyYAML", yaml_ok),
        ("其他依赖", deps_ok),
        ("OKX支持", okx_ok),
        ("config.yaml", config_ok),
    ]
    
    for name, status in all_checks:
        icon = "✅" if status else "❌"
        print(f"{icon} {name}")
    
    all_ok = all([status for _, status in all_checks])
    
    if all_ok:
        print("\n" + "="*60)
        print("🎉 所有检查通过！系统可以运行")
        print("="*60)
        print("\n下一步:")
        print("  python3 main.py --run-loop --interval 60")
    else:
        print("\n" + "="*60)
        print("⚠️ 发现问题，需要修复")
        print("="*60)
        
        if not ccxt_ok or not deps_ok:
            provide_installation_guide(pip_cmd)

if __name__ == "__main__":
    main()