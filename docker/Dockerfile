# iTop 3.3.0-1 生产镜像（jolleykong/iTop tag:3.3.0-1）
# 构建上下文 = 仓库根目录：
#   docker build -f docker/Dockerfile -t itop-web:3.3.0-1 .
FROM php:8.2-apache-bookworm

ENV DEBIAN_FRONTEND=noninteractive

# ── 系统依赖 + PHP 扩展（iTop 3.3 setup 要求：mysqli/gd/soap/curl/zip/intl/mbstring/ldap 等）──
RUN apt-get update && apt-get install -y --no-install-recommends \
      libfreetype6-dev libjpeg62-turbo-dev libpng-dev \
      libzip-dev zip unzip \
      libldap2-dev libicu-dev libonig-dev \
      libxml2-dev libcurl4-openssl-dev libssl-dev \
      default-mysql-client \
    && docker-php-ext-configure gd --with-freetype --with-jpeg \
    && docker-php-ext-install -j"$(nproc)" \
      gd mysqli pdo_mysql soap curl zip intl mbstring ldap opcache \
    && a2enmod rewrite headers \
    && echo 'ServerName localhost' > /etc/apache2/conf-available/servername.conf \
    && a2enconf servername \
    && sed -i 's/AllowOverride None/AllowOverride All/' /etc/apache2/apache2.conf \
    && rm -rf /var/lib/apt/lists/*

# PHP 运行参数（setup 与数据导入/同步需求）
RUN { \
      echo 'upload_max_filesize=64M'; \
      echo 'post_max_size=64M'; \
      echo 'memory_limit=512M'; \
      echo 'max_execution_time=300'; \
      echo 'date.timezone=Asia/Shanghai'; \
    } > /usr/local/etc/php/conf.d/itop.ini

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

# ── 源码 + composer 依赖（git 源码不含 vendor，需安装）──
COPY . /var/www/html
WORKDIR /var/www/html
RUN composer install --no-dev --no-interaction --optimize-autoloader --no-progress \
    && chown -R www-data:www-data conf data log \
    && chmod -R u+rwX,go+rX conf data log

EXPOSE 80
